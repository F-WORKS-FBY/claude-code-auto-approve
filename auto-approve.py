#!/usr/bin/env python3
"""Claude Code 的 PermissionRequest 钩子：要弹授权窗时，替你点掉"机械确认"类的。

  · 白名单里的工具（Bash/PowerShell 命令、文件编辑/写入、读文件/搜索、网络、MCP/Workflow、Skill）才会自动放行
  · 命令命中黑名单（DENY_PATTERNS）或删除目标危险（dangerous_rm）的，照常弹窗留给你
  · 计划确认（ExitPlanMode）和选择题（AskUserQuestion）永远不碰
  · 放行只对这一次有效，等于点「1 Yes」，不会留下"不再询问"的规则
不输出任何东西 = 照常弹窗。脚本出错、超时也是照常弹窗，不会误放行。

开关文件，改完立刻生效，不用重启会话：
  <配置目录>/auto-approve.mode   on（文件不存在也算 on）/ off（暂停）/ dryrun（只记日志不放行）
                                 写了别的按 off 处理
日志：<配置目录>/hooks/auto-approve.log（放行的、跳过的、试运行的都记）
<配置目录> 是这个脚本所在的 hooks 目录的上一级（安装后就是 ~/.claude 或 $CLAUDE_CONFIG_DIR）。

Linux / macOS / Windows 通用，只用标准库。安装、说明见 README.md。
"""
import json
import os
import posixpath
import re
import sys
import time

# ============================ 配置区（DIY 改这里） ============================

# 白名单：工具名完整匹配其中一条（正则）才会自动放行。
# 加了工具，还要把它加进 settings.json 里这个钩子的 matcher（install.py 里的 MATCHER），否则钩子收不到
ALLOW_TOOLS = {
    "Bash命令": r"Bash|PowerShell",   # Windows 上没装 Git Bash 时，Claude 用 PowerShell 工具跑命令
    "文件编辑": r"Edit|MultiEdit|NotebookEdit",   # MultiEdit 只有较早的 Claude Code 版本才有
    "文件写入": r"Write",
    "读文件/搜索": r"Read|Glob|Grep",
    "网络": r"WebFetch|WebSearch",
    "通用工具": r"mcp__.+|Workflow",
    "Skill": r"Skill",
}

# 永远留给你：计划确认（要看方案、可能要改）、Claude 给你出的选择题。
# 白名单和 matcher 以后就算放宽成 .*，这两个也不会被放行
NEVER_TOOLS = {
    "ExitPlanMode": "计划确认，留给你看方案",
    "AskUserQuestion": "选择题，留给你选",
}

# 只有这两类工具的参数会拿去查黑名单和危险删除：
#   · Bash / PowerShell：只查要执行的命令本身（不查 Claude 写的说明）
#   · MCP 工具：查除文件内容以外的所有参数（比如数据库工具的 SQL）
# 文件编辑/写入、读文件、搜索、网络、Skill 只看工具类型；Workflow 的脚本只是编排，子代理执行的每一步还会各自经过这个钩子
SHELL_TOOLS = {"Bash", "PowerShell"}
CONTENT_KEYS = {"content", "old_string", "new_string", "new_source", "edits"}   # 文件内容：改一份写着 sudo 的文档不该被拦

# 搜索类命令只是在找文字，不执行：这一段命令整段不查（带 $(...) 或反引号的除外）
SEARCH_COMMANDS = {"grep", "egrep", "fgrep", "rg", "ag", "ack", "findstr", "select-string", "sls"}
GIT_SEARCH_SUBCOMMANDS = {"grep", "log", "show", "diff", "blame"}

# git 和子命令之间允许出现全局参数，比如 git -C /repo push、git -c k=v push、git --no-pager push
GIT = r"""\bgit(?:\s+(?:-[Cc]\s*(?:(?:[^\s'"]*(?:'[^']*'|"[^"]*"))+[^\s'"]*|\S+)|--[\w-]+(?:=\S+)?))*\s+"""

# 黑名单：命令里出现其中一条，就不自动放行（正则，不区分大小写）
# rm 不在这里一刀切，而是解析出删除目标、只拦"删了就是灾难"的（见 dangerous_path）：
#   根目录、家目录及其下一层（~/x、/home/me/x、/Users/me/x、C:\Users\me\x 都算）、. / .. / *、
#   两层以内的绝对路径（如 /volume/coder；Windows 从盘符往下数）、/tmp 本身或 /tmp/*、
#   X/* 和删 X 本身同等对待、变量为空就会变成上述路径的写法（如 $DIR/、"$DIR"/*）、删除目标是命令输出（$(...)）。
#   变量若在命令里先赋过值（如 D=/x/y; rm -rf $D）就代入实际路径判断；没赋过的按系统环境变量，再没有就按空值做最坏打算。
#   PowerShell 的 Remove-Item（及 ri / rd / rmdir / del / erase）按同样的规则解析目标。
DENY_PATTERNS = [
    # 普通 git push 放行；只拦会改写/删除远程历史的：-f（含 -uf 这类组合）、--force*、--delete、-d、--mirror、+分支、:分支
    GIT + r"""push\b[^;&|\n]*?\s["']?(?:-[a-zA-Z]*[fd][a-zA-Z]*(?![\w-])|--force\S*|--delete\b|--mirror\b|\+\S|:\S)""",
    GIT + r"reset\b[^;&|\n]*\s--hard\b",
    GIT + r"clean\b(?![^;&|\n]*\s(?:-[a-zA-Z]*n[a-zA-Z]*|--dry-run)\b)",   # git clean -n（只预览）放行
    GIT + r"stash\s+(?:drop|clear)\b",
    r"\bsudo\b",
    r"\b(?:mkfs|fdisk|parted)\b",
    r"\bdd\b[^;&|\n]*\bof=(?!/dev/null\b)",              # 只拦往外写的 dd（写盘、写镜像）；只读和写到 /dev/null 的放行
    r"(?<![\w-])(?:shutdown|reboot|poweroff|halt)\b(?![\w.-])",
    r"\bchmod\b[^;&|\n]*\s0?777\b",
    r"\b(?:drop|truncate)\s+(?:table|database|schema)\b",
    r"\bkubectl\b[^;&|\n]*?\sdelete\b(?![^;&|\n]*--dry-run)",
    r"\bdocker(?:-compose|\s+compose)?\b[^;&|\n]*?\s(?:rmi?|(?:container|image|volume|network|system|builder)\s+(?:rm|prune)|down\b[^;&|\n]*\s(?:-v|--volumes))\b",
    r"\b(?:p?npm|yarn)\s+publish\b(?![^;&|\n]*--dry-run)",
    r"\b(?:Format-Volume|Clear-Disk|Stop-Computer|Restart-Computer)\b",
]

# ==========================================================================

HOME = os.path.expanduser("~")
HERE = os.path.dirname(os.path.abspath(__file__))
if os.path.basename(HERE) == "hooks":
    CONFIG_DIR = os.path.dirname(HERE)   # 装在 <配置目录>/hooks/ 下：开关和日志跟着脚本走，不依赖环境变量
else:
    CONFIG_DIR = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude")
MODE_FILE = os.environ.get("AUTO_APPROVE_MODE_FILE") or os.path.join(CONFIG_DIR, "auto-approve.mode")
LOG_FILE = os.environ.get("AUTO_APPROVE_LOG") or os.path.join(CONFIG_DIR, "hooks", "auto-approve.log")
LOG_MAX_BYTES = 5 * 1024 * 1024   # 超过就挪成 .log.1 重新记

DELETE_COMMANDS = {"rm", "remove-item", "ri", "rd", "rmdir", "del", "erase"}
BUILTIN_ENV = {"HOME": "~", "USERPROFILE": "~", "TMPDIR": "/tmp", "TMP": "/tmp", "TEMP": "/tmp", "PWD": ".", "OLDPWD": "."}
ENV_ASSIGN = re.compile(r"([A-Za-z_]\w*)=(.*)", re.S)
VAR = re.compile(r"\$\{(?:env:)?(\w+)(?:(:?[-=?+])([^}]*))?\}|\$(?:env:)?(\w+)", re.I)
HOME_ROOT = re.compile(r"(?:/home/[^/]+|/Users/[^/]+|[A-Za-z]:/Users/[^/]+|/root)(?=/|$)", re.I)


def _norm(path):
    p = re.sub(r"/+", "/", path.replace("\\", "/"))
    return re.sub(r"^/([A-Za-z])(?=/|$)", r"\1:", p)   # Git Bash 写法 /c/Users/me → c:/Users/me


MY_HOME = _norm(HOME).rstrip("/")


# ---------------------------- 切命令 ----------------------------

def split_segments(cmd):
    """按 ; & | 换行 括号把命令切成段，引号、$(...) 里面的不切。返回 [(这一段的原文, [词, ...])]，词保留引号"""
    segs, words, word = [], [], ""
    start, i, n, quote = 0, 0, len(cmd), None
    while i < n:
        c = cmd[i]
        if quote:
            word += c
            if c == "\\" and quote == '"' and i + 1 < n:
                word += cmd[i + 1]
                i += 1
            elif c == quote:
                quote = None
        elif c in "'\"`":
            quote = c
            word += c
        elif c == "\\" and i + 1 < n:
            word += c + cmd[i + 1]
            i += 1
        elif c == "$" and cmd[i + 1:i + 2] == "(":   # $(...) 整个算进当前这个词
            depth, j = 0, i + 1
            while j < n:
                depth += {"(": 1, ")": -1}.get(cmd[j], 0)
                if depth == 0:
                    break
                j += 1
            word += cmd[i:j + 1]
            i = j
        elif c in ";&|\n()":
            if word:
                words.append(word)
                word = ""
            if words:
                segs.append((cmd[start:i], words))
            words, start = [], i + 1
        elif c.isspace():
            if word:
                words.append(word)
                word = ""
        else:
            word += c
        i += 1
    if word:
        words.append(word)
    if words:
        segs.append((cmd[start:], words))
    return segs


def unquote(word):
    return re.sub(r"""(?<!\\)['"]""", "", word)


def quoted_parts(word):
    """词里引号括起来的内容（bash -c '...'、ssh host "..." 里真正要执行的命令）"""
    parts = re.findall(r"""'([^']*)'|"((?:\\.|[^"\\])*)"|`([^`]*)`""", word)
    return "\n".join(a or b.replace('\\"', '"') or c for a, b, c in parts)


def is_search(words):
    """这一段是不是只在搜文字：grep / rg / git grep / git log 之类"""
    if any("$(" in w or "`" in w for w in words):
        return False
    plain = [unquote(w) for w in words if not ENV_ASSIGN.fullmatch(unquote(w))]
    if not plain:
        return False
    first = re.sub(r"^.*[\\/]", "", plain[0]).lower()
    if first in SEARCH_COMMANDS:
        return True
    if first == "git":
        sub = next((w for w in plain[1:] if not w.startswith("-")), "")
        return sub in GIT_SEARCH_SUBCOMMANDS
    return False


def command_text(cmd):
    """要拿去查黑名单的命令文字：接上续行，去掉只是在搜索文字的那几段"""
    cmd = re.sub(r"[\\`]\r?\n", " ", cmd)
    return "\n".join(raw for raw, words in split_segments(cmd) if not is_search(words))


# ---------------------------- 删除目标 ----------------------------

def home_rest(p):
    """p 在某个家目录里时，返回家目录之后的部分（'' 表示就是家目录本身），否则 None"""
    low = p.lower()
    if MY_HOME and (low == MY_HOME.lower() or low.startswith(MY_HOME.lower() + "/")):
        return p[len(MY_HOME):]
    m = HOME_ROOT.match(p)
    return p[m.end():] if m else None


def dangerous_path(path):
    """删了就是灾难的路径（规则见配置区说明）。空路径 = 变量为空、rm 没拿到参数，什么也不删"""
    p = _norm(path.strip())
    if not p:
        return False
    if p in ("*", ".*"):
        p = "."
    elif p.endswith(("/*", "/.*")):                            # X/* 和删 X 本身一样危险
        p = p[:p.rfind("/")] or "/"
    drive = ""
    if re.match(r"[A-Za-z]:", p):
        drive, p = p[:2], p[2:]
        if not p:
            return True                                          # 裸盘符 C: 是那个盘的当前目录
    if p.startswith("~"):                                        # ~、~/x、~user
        return len([x for x in posixpath.normpath("/" + p[1:]).split("/") if x]) <= 1
    if not p.startswith("/"):
        return all(x in ("", ".", "..") for x in posixpath.normpath(p).split("/"))   # . ./ .. ../.. build/..
    p = posixpath.normpath(p)                                    # 解析 /a/../b 这类写法
    rest = home_rest(drive + p)
    if rest is not None:
        return len([x for x in rest.split("/") if x]) <= 1
    if not drive and re.match(r"(?:/private)?(?:/var)?/tmp(?:/|$)", p, re.I):
        return re.fullmatch(r"(?:/private)?(?:/var)?/tmp", p, re.I) is not None   # /tmp 本身；/tmp 下的子目录放行
    return len([x for x in p.split("/") if x]) <= 2


def expand(s, env, depth=0):
    """展开 $X、${X}、${X:-默认}、${X:?}、$env:X"""
    def sub(m):
        name = m.group(1) or m.group(4)
        op, alt = m.group(2) or "", m.group(3) or ""
        val = env.get(name, env.get(name.upper()))
        if val is None:
            val = os.environ.get(name)
        if op.endswith("+"):
            return alt if val else ""
        if (val is None or (val == "" and op.startswith(":"))) and op.lstrip(":") in ("-", "="):
            return expand(alt, env, depth + 1) if depth < 3 else alt
        return val or ""                                         # 不认识的变量按空值
    return VAR.sub(sub, s)


def dangerous_rm(cmd, depth=0):
    """从命令里找出所有删除命令的目标，有危险的就返回它（如 "rm $DIR/"），否则返回 None"""
    env = dict(BUILTIN_ENV)
    for _, words in split_segments(re.sub(r"[\\`]\r?\n", " ", cmd)):
        if depth < 2:                                            # bash -c '...'、ssh host "..." 里的命令也要看
            for w in words:
                inner = quoted_parts(w)
                hit = inner and dangerous_rm(inner, depth + 1)
                if hit:
                    return hit
        for i, w in enumerate(words):
            plain = unquote(w)
            m = ENV_ASSIGN.fullmatch(plain)
            if m:
                env[m.group(1)] = expand(m.group(2), env)
                continue
            name = re.sub(r"^.*[\\/]", "", plain).lower()
            if name not in DELETE_COMMANDS:
                continue
            args = words[i + 1:]
            before = [re.sub(r"^.*[\\/]", "", unquote(x)) for x in words[:i]]
            if name == "rm" and "git" in before and "--cached" in map(unquote, args):
                break                                            # git rm --cached 只是不再跟踪，文件还在
            if "xargs" in before:
                return "xargs %s" % plain                        # 删除目标来自前面命令的输出
            for arg in args:
                if not arg or arg.startswith("-") or re.search(r"[<>]", arg):   # 选项、重定向
                    continue
                if "$(" in arg or "`" in arg:                    # 删除目标是命令输出，事先不知道删的是什么
                    return "%s %s" % (plain, arg)
                if dangerous_path(expand(unquote(arg), env)):
                    return "%s %s" % (plain, arg)
            break
    return None


# ---------------------------- 判定 ----------------------------

def strings(value, key=None):
    """参数里所有的字符串（命令、路径、说明……），跳过文件内容"""
    if key in CONTENT_KEYS:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield from strings(v, k)
    elif isinstance(value, list):
        for v in value:
            yield from strings(v, key)


def decide(tool, tool_input, permission_mode=""):
    """返回 (是否放行, 放行的类别 / 不放行的原因)"""
    if tool in NEVER_TOOLS:
        return False, NEVER_TOOLS[tool]
    category = next((k for k, v in ALLOW_TOOLS.items() if re.fullmatch(v, tool)), None)
    if not category:
        return False, "工具不在白名单"
    if permission_mode == "auto" and tool in SHELL_TOOLS:
        return False, "auto 模式下命令由 Claude Code 自己判断"   # 这时 Claude Code 不采纳钩子对命令的放行
    if tool in SHELL_TOOLS and tool_input.get("dangerouslyDisableSandbox"):
        return False, "要求在沙箱外运行"
    if tool in SHELL_TOOLS:
        cmd = tool_input.get("command")
        text = command_text(cmd if isinstance(cmd, str) else "")
    elif tool.startswith("mcp__"):
        text = "\n".join(strings(tool_input))
    else:
        return True, category
    for pattern in DENY_PATTERNS:
        m = re.search(pattern, text, re.I)
        if m:
            return False, "命中黑名单「%s」" % re.sub(r"\s+", " ", m.group(0)).strip()
    rm = dangerous_rm(text)
    if rm:
        return False, "危险删除「%s」" % rm
    return True, category


# ---------------------------- 开关、日志、入口 ----------------------------

def read_mode():
    try:
        with open(MODE_FILE, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return "on"
    except OSError:
        return "off"
    # Windows PowerShell 5 的 `echo on > 文件` 写出来是带 BOM 的 UTF-16
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig", "replace")
    mode = text.strip().lower()
    return mode if mode in ("on", "off", "dryrun") else "off"


def short(s, n=160):
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s if len(s) <= n else s[:n] + "…"


def describe(event, tool, tool_input):
    """日志里的一行：哪个会话、什么工具、命令/路径是什么"""
    cwd = event.get("cwd") or ""
    if cwd == HOME or cwd.startswith((HOME + "/", HOME + "\\")):
        cwd = "~" + cwd[len(HOME):]
    for key in ("command", "file_path", "notebook_path", "url", "query", "pattern", "skill"):
        if isinstance(tool_input.get(key), str):
            detail = tool_input[key]
            break
    else:
        detail = json.dumps(tool_input, ensure_ascii=False)
    return "〔%s · %s〕%s：%s" % (cwd, (event.get("session_id") or "")[:8], tool, short(detail))


def log(msg):
    line = "[%s] %s\n" % (time.strftime("%m-%d %H:%M:%S"), msg)
    try:
        if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            os.replace(LOG_FILE, LOG_FILE + ".1")
    except OSError:
        pass
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass


def main():
    try:
        # 按字节读再当 UTF-8 解：中文 Windows 上 stdin 默认按 GBK 解码，会把中文后面紧跟的
        # \n 之类吃掉，「# 清理\nrm -rf ~」就读成了一行注释，危险删除漏判
        event = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    except Exception:
        return  # 解析不了就照常弹窗
    mode = read_mode()
    if mode == "off":
        return
    tool = event.get("tool_name") or ""
    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        tool_input = {}
    ok, why = decide(tool, tool_input, event.get("permission_mode") or "")
    what = describe(event, tool, tool_input)
    if not ok:
        log("跳过（%s）%s" % (why, what))
        return
    if mode == "dryrun":
        log("[试运行] 会自动确认（%s）%s" % (why, what))
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }))
    log("已自动确认（%s）%s" % (why, what))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass  # 出任何错都照常弹窗
    sys.exit(0)
