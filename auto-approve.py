#!/usr/bin/env python3
"""Claude Code 的 PermissionRequest 钩子：要弹授权窗时，替你点掉"机械确认"类的。

  · 白名单里的工具（Bash/PowerShell 命令、文件编辑/写入、读文件/搜索、网络、MCP/Workflow、Skill）才会自动放行
  · 命令命中黑名单（DENY_PATTERNS）或删除目标危险（dangerous_rm）的，照常弹窗留给你
  · sudo 后面跟的是只读命令（SUDO_READ_ONLY）时放行，其它提权照样留给你
  · Windows（含 WSL）上涉及 C 盘（PROTECTED_DRIVES）的操作，除了读，一律留给你
  · 计划确认（ExitPlanMode）和选择题（AskUserQuestion）永远不碰
  · 放行只对这一次有效，等于点「1 Yes」，不会留下"不再询问"的规则
不输出任何东西 = 照常弹窗。脚本出错、超时也是照常弹窗，不会误放行。
Windows 上还会作为 PreToolUse 钩子登记：涉及 C 盘的写操作强制弹窗，acceptEdits 模式下本来不问的编辑也会问。

开关文件，改完立刻生效，不用重启会话：
  <配置目录>/auto-approve.mode   on（文件不存在也算 on）/ off（暂停）/ dryrun（只记日志不放行）
                                 写了别的按 off 处理
日志：<配置目录>/hooks/auto-approve.log（放行的、跳过的、试运行的都记）
<配置目录> 是这个脚本所在的 hooks 目录的上一级（安装后就是 ~/.claude 或 $CLAUDE_CONFIG_DIR）。

Linux / macOS / Windows 通用，只用标准库。安装、说明见 README.md。
"""
import json
import os
import platform
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

# 喂给解释器的代码（heredoc、PowerShell 的 @'…'@、-c / -e 后面那段）当数据，不按 shell 命令查——
# 除非代码里会去执行命令：出现下面这些调用，就整段照常检查
INTERPRETERS = {"python": "python", "py": "python", "pypy": "python", "node": "node", "nodejs": "node",
                "bun": "node", "deno": "node", "ruby": "other", "perl": "other", "php": "other"}
EXEC_CALLS = {
    "python": r"\bsubprocess\b|\bos\.(?:system|popen|exec\w*|spawn\w*|startfile|posix_spawn\w*)\b|\bpty\.spawn\b"
              r"|\bcreate_subprocess_\w+|\bcommands\.get\w+|\b(?:exec|eval)\s*\(|__import__",
    "node": r"\bchild_process\b|\b(?:exec|execFile|spawn|fork)(?:Sync)?\s*\(|\bDeno\.(?:run|Command)\b|\bBun\.(?:spawn\w*|\$)"
            r"|\beval\s*\(",
    "other": r"`|%x|\bqx\b|\b(?:system|exec|spawn|shell_exec|passthru|proc_open|popen|eval)\s*\(|\bOpen3\b|\bIO\.popen\b",
}

# git 和子命令之间允许出现全局参数，比如 git -C /repo push、git -c k=v push、git --no-pager push
GIT = r"""\bgit(?:\s+(?:-[Cc]\s*(?:(?:[^\s'"]*(?:'[^']*'|"[^"]*"))+[^\s'"]*|\S+)|--[\w-]+(?:=\S+)?))*\s+"""
END = r"\s*(?=$|[;&|\n)])"   # 这一段命令到此为止，后面没有别的参数

# 只读的 git：只认 -C 路径、--no-pager 这类全局参数。-c 配置项=值 不认——core.fsmonitor、core.pager 这类配置会执行程序
GIT_READ = (r"""git(?:\s+(?:(?-i:-C)\s*(?:(?:[^\s'"]*(?:'[^']*'|"[^"]*"))+[^\s'"]*|\S+)|(?-i:-P)|--no-pager|--no-optional-locks"""
            r"""|--git-dir=\S+|--work-tree=\S+))*\s+""")

# 只读命令，分两份。每条是正则，从命令名开始匹配，不区分大小写。
#
# SUDO_READ_ONLY：sudo / doas / gsudo 后面跟的是这些命令时放行——这是以 root 身份运行，只放真正只看不改的。
# 往这里加命令前，先查清它有没有这几类用法，有就用负向断言排除掉：
#   · 输出到文件：-o、--output、w 命令……（写进 /etc/cron.d 就等于执行命令）
#   · 执行别的程序：--pre、--pager、--compress-program、e 命令……
#   · 改系统状态：date -s、hostname 新名字、dmesg -n……
SUDO_READ_ONLY = [
    # 文件、目录、磁盘、系统信息
    r"(?:ls|ll|dir|pwd|cd|cat|type|head|tail|less|more|wc|stat|du|df|lsblk|free|uptime|whoami|id|uname|which|where|whereis|printenv|echo|printf|true)\b",
    r"tree\b(?![^;&|\n]*\s(?-i:-[a-zA-Z]*o)\b)",                                # tree -o：结果写进文件
    r"file\b(?![^;&|\n]*\s(?:(?-i:-[a-zA-Z]*C)|--compile)\b)",                   # file -C：写出编译好的 magic 文件
    r"date\b(?![^;&|\n]*\s(?:(?-i:-[a-zA-Z]*s)|--set)\b)(?![^;&|\n]*\s\d{8,}(?:\.\d\d)?(?=\s|$))",   # date -s / date 0101…：改系统时间
    r"hostname\b(?![^;&|\n]*\s(?:(?-i:-[a-zA-Z]*[Fb])|--file|--boot)\b)(?:\s+-[a-zA-Z]+|\s+--[\w-]+)*" + END,   # 带新名字就是改主机名
    r"(?:grep|egrep|fgrep|zgrep|zcat|findstr)\b",
    r"(?:rg|ag|ack)\b(?![^;&|\n]*\s--(?:pre|pager)\b)",                         # --pre / --pager：执行程序
    r"find\b(?![^;&|\n]*\s-(?:delete|exec|execdir|ok|okdir|fprint\w*|fls)\b)",
    # 进程、端口、防火墙、服务、日志（Linux / macOS / Windows）
    r"(?:ps|pgrep|netstat|lsof|nslookup|dig)\b",
    r"ss\b(?![^;&|\n]*\s(?:(?-i:-[a-zA-Z]*K)|--kill)\b)",                        # ss -K：断开连接
    r"ipconfig\b(?![^;&|\n]*\s[/-](?:release|renew|flushdns|registerdns|setclassid))",
    r"ufw\s+status\b",
    r"firewall-cmd\s+--(?:state|list|get|query)(?![^;&|\n]*\s--(?:add|remove|change|set|new|delete|reload|complete-reload|runtime-to-permanent|panic|lockdown)\b)",
    r"ip6?tables(?:-legacy|-nft)?\b(?=[^;&|\n]*\s(?-i:-[a-zA-Z]*[LS]\b|--list\b|--list-rules\b))"
    r"(?![^;&|\n]*\s(?-i:-[a-zA-Z]*[FADIXPZNER]\b|--(?:flush|append|delete|insert|policy|zero|new-chain|rename-chain|replace)\b))",
    r"ip6?tables-save\b",
    r"nft\s+(?:-\S+\s+)*list\b",
    r"pfctl\s+-s\s*\w+" + END,                                                    # 只认 pfctl -s 类别，后面带 -F 之类的不算
    r"systemctl\s+(?:status|is-active|is-enabled|is-failed|list-\S+|show|cat)\b",
    r"journalctl\b(?![^;&|\n]*--(?:vacuum|rotate|flush|sync|relinquish|setup-keys|update-catalog))",
    r"dmesg\b(?![^;&|\n]*\s(?:(?-i:-[a-zA-Z]*[cCnDE])\b|--(?:clear|read-clear|console-\S+)\b))",   # -c 清空、-n/-D/-E 改控制台日志
    r"launchctl\s+(?:list|print)\b",
    r"log\s+show\b",
    # 容器的查询：docker / podman / nerdctl / crictl 都认，没装 docker 的机器不受影响。
    # config 只在 compose 后面认：docker config 本身是管理 swarm 配置的（create / rm）
    r"(?:docker|podman|nerdctl)\s+(?:ps|images|logs|inspect|info|version|stats|top|port|events|history|diff)\b",
    r"(?:docker|podman|nerdctl)\s+compose\s+(?:ps|ls|images|logs|top|port|events|version|config\b(?![^;&|\n]*\s(?:(?-i:-o)|--output)\b))\b",
    r"(?:docker|podman|nerdctl)\s+(?:container|image|volume|network|system|context|pod)\s+(?:ls|list|ps|inspect|df|info|logs|top|port)\b",
    r"crictl\s+(?:ps|pods|images|img|inspect\w*|info|logs|stats|version)\b",
    # git、包管理器的查询。git 的 --output 写文件，git grep -O 用指定的程序打开文件
    GIT_READ + r"(?:status|log|diff|show|blame|grep|ls-files|ls-tree|rev-parse|describe|shortlog|cat-file|whatchanged)\b"
    r"(?![^;&|\n]*\s(?:--output\b|(?-i:-O)|--open-files-in-pager\b))",
    GIT_READ + r"(?:branch|tag|remote)(?:\s+(?:-a|-r|-v+|-l|--list|--all|--remotes|--show-current|--verbose))*" + END,
    GIT_READ + r"(?:stash\s+list|config(?:\s+--(?:global|system|local|worktree)|\s+--file\s+\S+)*\s+(?:--get\S*|--list|-l))\b",
    r"(?:npm|pnpm|yarn)\s+(?:ls|list|view|info|outdated|why)\b",
    r"(?:pip3?|python3?\s+-m\s+pip)\s+(?:list|show|freeze|check)\b",
    # PowerShell：Get-* / Test-* 这类只读命令（sort 在 bash 里是 GNU sort，不放在这里）
    r"(?:Get-\w+|Test-\w+|Resolve-Path|Select-String|Measure-Object|Select-Object|Sort-Object|Where-Object|Format-\w+|Out-String|Write-Output|Write-Host|ConvertTo-\w+|ConvertFrom-\w+|Compare-Object|Group-Object|gci|gc|gi|gp|gps|gsv|gcm|sls|select|measure|ft|fl)\b",
]

# 文本处理工具：读进来、打到屏幕上。只用在 Windows 的 C 盘只读判断里，sudo 后面不放行。
# 会写文件、执行程序的用法除外：sort -o / --compress-program、yq -i / -s、awk 的 -i inplace / -l / -f / system() / >，
# uniq / xxd 的第二个文件参数；sed 除了 -i / -f，还会解析脚本，见 sed_safe
TEXT_READ_ONLY = [
    r"(?:cut|tr|jq|column|nl|rev|tac|od|hexdump|strings|cmp|diff|comm|paste|fold|basename|dirname|realpath|readlink|cygpath"
    r"|md5sum|sha\d*sum|cksum|test|false|pushd|popd|tasklist|systeminfo|ver)\b",
    r"yq\b(?![^;&|\n]*\s(?:(?-i:-[a-zA-Z]*[is])\b|--inplace\b|--split-exp\b))",
    r"sort\b(?![^;&|\n]*\s(?:(?-i:-[a-zA-Z]*o)|--output|--compress-program)\b)",
    r"sed\b(?![^;&|\n]*\s(?:(?-i:-[a-zA-Z]*[if])\b|--in-place\b|--file\b))",
    r"(?:awk|gawk|mawk|nawk)\b(?![^;&|\n]*(?:\s(?-i:-[a-zA-Z]*[ilfE])\b|\s--(?:include|load|file|exec)\b|system\s*\(|>|\||@load|@include))",
    r"(?:uniq|xxd)(?:\s+-\S+)*(?:\s+[^\s;&|-]\S*)?" + END,
]

# Windows 的 C 盘只读判断用的完整清单。另外，不带路径的 xxx --version / --help 也算只读（带路径的可能是任意程序）
READ_ONLY_COMMANDS = SUDO_READ_ONLY + TEXT_READ_ONLY
BARE_VERSION = r"[\w.+-]+\s+(?:--version|--help)" + END

# Windows（包括 WSL 里的 /mnt/c）上，涉及这些盘的操作，除了读，一律留给你。不需要就改成 set()
PROTECTED_DRIVES = {"C"}

# 不提路径也会写进用户目录 / 系统的命令：装软件、改全局配置、改注册表和环境变量。
# 家目录在受保护的盘上时（Windows 上通常就是 C 盘），这些命令也算涉及受保护的盘
USER_PROFILE_WRITERS = [
    r"\b(?:pip3?|python3?(?:\.\d+)?\s+-m\s+pip|py(?:\s+-[\d.]+)?\s+-m\s+pip|uv\s+pip|conda|mamba)\s+(?:install|uninstall|remove|create|update|upgrade)\b",
    r"\b(?:npm|pnpm|yarn)\b[^;&|\n]*\s(?:-g|--global)\b|\byarn\s+global\b|\b(?:npm|pip)\s+config\s+set\b",
    r"\b(?:winget|choco|scoop)\s+(?:install|uninstall|upgrade|update|remove)\b",
    r"\b(?:pipx|cargo|go|gem)\s+install\b|\buv\s+tool\s+install\b|\bdotnet\s+tool\s+install\b",
    r"\bsetx\b|\breg(?:\.exe)?\s+(?:add|delete|import|copy|restore|load)\b|\bschtasks\b[^;&|\n]*\s/(?:create|delete|change)\b"
    r"|\bsc(?:\.exe)?\s+(?:create|delete|config)\b",
    GIT + r"config\b(?![^;&|\n]*\s(?:--get\S*|--list|-l)\b)[^;&|\n]*\s--(?:global|system)\b",   # --get / --list 只是读
    r"\bclaude\s+(?:config|mcp|plugin)\b",
    r"\b(?:Set|New|Remove)-ItemProperty\b|\bInstall-(?:Module|Package|Script)\b|SetEnvironmentVariable",
]

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
    # 提权：sudo / doas / gsudo 后面跟的不是只读命令（见 SUDO_READ_ONLY）就留给你；pkexec、以管理员身份启动一律留给你
    r"\b(?:sudo|doas|gsudo)\b(?!(?:\s+(?:-(?![A-Za-z]*[ugpCDhrtUT])[A-Za-z]+|-[A-Za-z]*[ugpCDhrtUT]\s*\S+|--[\w-]+(?:=\S+)?|[A-Za-z_]\w*=\S*))*"
    r"\s+(?:\S*[\\/])?(?:" + "|".join(SUDO_READ_ONLY) + r"))",
    r"\bpkexec\b",
    r"\bStart-Process\b[^;&|\n]*\s-Verb\s+RunAs\b",
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


def _platform():
    """windows / wsl / posix。AUTO_APPROVE_PLATFORM 可以强制指定（测试用）"""
    forced = os.environ.get("AUTO_APPROVE_PLATFORM")
    if forced:
        return forced
    if os.name == "nt":
        return "windows"
    if os.environ.get("WSL_DISTRO_NAME") or "microsoft" in platform.release().lower():
        return "wsl"
    return "posix"


PLATFORM = _platform()
READ_ONLY = re.compile(r"(?:\S*[\\/])?(?:%s)|%s" % ("|".join(READ_ONLY_COMMANDS), BARE_VERSION), re.I)
DRIVE_REF = re.compile(r"""(?:^|[\s'"=(,;])(?:([A-Za-z]):(?:[\\/]|(?=[\s'"]|$))|/(?:mnt/)?([A-Za-z])/)""")
ENV_REF = re.compile(r"\$\{?(?:env:)?(\w+)\}?|%(\w+)%", re.I)
REDIRECT = re.compile(r"(?<![<>])\d?>>?\s*(?!&\d|>)([^\s;&|]+)")


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


HEREDOC = re.compile(r"""<<(-?)[ \t]*(?:'([^'\n]+)'|"([^"\n]+)"|(\\?)([A-Za-z_][\w.-]*))""")
HERE_STRING = re.compile(r"""@(['"])\r?\n(.*?)\r?\n\1@([ \t]*\|[ \t]*[^\r\n;|]+)""", re.S)
INLINE_CODE_FLAGS = {"-c", "-e", "-E", "-r", "-p", "--eval", "--print"}


def interpreter(word):
    """这个词是不是 Python / Node / Ruby / Perl / PHP 这类解释器，是就返回它属于哪一类"""
    name = re.sub(r"^.*[\\/]", "", unquote(word)).lower()
    name = re.sub(r"(?:\.exe)?$", "", name)
    return INTERPRETERS.get(re.sub(r"[\d.]+$", "", name))


def inert(code, family, expands):
    """这段代码不会去执行命令；expands=True 表示 shell 会先展开里面的 $(...) 和反引号"""
    return not re.search(EXEC_CALLS[family], code) and not (expands and re.search(r"\$\(|`", code))


def strip_inert_code(cmd):
    """把喂给解释器、又不会执行命令的 heredoc 正文和 PowerShell here-string 拿掉，剩下的照常检查"""
    out, i = "", 0
    while True:
        m = HEREDOC.search(cmd, i)
        line_end = cmd.find("\n", m.end()) if m else -1
        if not m or line_end < 0:
            out += cmd[i:]
            break
        delim = m.group(2) or m.group(3) or m.group(5)
        quoted = bool(m.group(2) or m.group(3) or m.group(4))   # <<'EOF' <<"EOF" <<\EOF：shell 不展开正文
        start = end = line_end + 1
        while end < len(cmd):                                   # 正文到单独一行的结束标记为止
            nxt = cmd.find("\n", end)
            nxt = len(cmd) if nxt < 0 else nxt
            line = cmd[end:nxt].rstrip("\r")
            if (line.lstrip("\t") if m.group(1) else line) == delim:
                break
            end = nxt + 1
        end = min(end, len(cmd))
        before = re.split(r"[;&|]", cmd[cmd.rfind("\n", 0, m.start()) + 1:m.start()])[-1]
        family = next((f for f in map(interpreter, before.split()) if f), None)
        body = cmd[start:end]
        out += cmd[i:start] + ("" if family and inert(body, family, not quoted) else body)
        i = end
    def here_string(m):
        family = next((f for f in map(interpreter, m.group(3).replace("|", " ").split()) if f), None)
        return "@''@" + m.group(3) if family and inert(m.group(2), family, m.group(1) == '"') else m.group(0)
    return HERE_STRING.sub(here_string, out)


def strip_inline_code(raw, words):
    """python -c '…'、node -e '…' 这类一行代码，不会执行命令的就从这一段里拿掉"""
    for i, w in enumerate(words):
        family = interpreter(w)
        if not family:
            continue
        for j in range(i + 1, len(words) - 1):
            flag = unquote(words[j])
            if flag in INLINE_CODE_FLAGS:
                code = words[j + 1]
                if inert(quoted_parts(code) or unquote(code), family, not code.startswith("'")):
                    raw = raw.replace(code, "''", 1)
                break
            if not flag.startswith("-"):
                break
        break
    return raw


def command_text(cmd):
    """要拿去查黑名单的命令文字：去掉不会执行命令的解释器代码，接上续行，去掉只是在搜索文字的那几段"""
    cmd = re.sub(r"[\\`]\r?\n", " ", strip_inert_code(cmd))
    return "\n".join(strip_inline_code(raw, words) for raw, words in split_segments(cmd) if not is_search(words))


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


# ---------------------------- Windows 的 C 盘 ----------------------------

SHELL_WRAPPERS = {"bash", "sh", "zsh", "dash"}
PS_WRAPPERS = {"powershell", "pwsh"}
# PowerShell 的自动变量（$_ 是管道里的当前对象），不是环境变量，别拿去查环境
PS_AUTOMATIC = {"_", "null", "true", "false", "args", "input", "this", "psitem", "matches", "error", "host", "lastexitcode", "pid"}

# PowerShell 只读判断：动词-名词形式的命令，只认这些读取类动词 / 命令；别名只认这些
PS_READ_VERBS = {"get", "test", "select", "where", "sort", "format", "measure", "group", "compare", "convertto",
                 "convertfrom", "resolve"}
PS_READ_CMDLETS = {"out-string", "out-host", "out-null", "out-default", "foreach-object", "write-output", "write-host",
                   "write-verbose", "write-warning", "write-debug", "write-information", "split-path", "join-path",
                   "set-location", "push-location", "pop-location"}
PS_READ_ALIASES = {"ls", "dir", "gci", "gc", "cat", "type", "echo", "pwd", "select", "where", "?", "sort", "measure",
                   "ft", "fl", "fw", "gm", "gp", "gi", "gv", "gal", "gcm", "gcim", "gwmi", "ps", "gps", "gsv", "sls", "%",
                   "foreach", "cd", "sl", "history", "h"}
PS_KEYWORDS = {"if", "elseif", "else", "foreach", "for", "while", "do", "until", "switch", "try", "catch", "finally",
               "return", "break", "continue", "param", "begin", "process", "end", "in", "default"}
PS_SAFE_TYPES = {"math", "string", "int", "int32", "int64", "long", "double", "decimal", "char", "bool", "datetime",
                 "timespan", "regex", "convert", "bitconverter", "guid", "version", "uri", "array", "text.encoding"}
# 文件、目录、路径、环境的静态方法，只认读取的（写入的 Write* / Set* / Delete… 由 PS_WRITE_METHOD 拦下）。
# Path 的方法都只是拼字符串，只有 GetTempFileName 会真的建一个文件
PS_READ_STATIC = {
    "io.file": r"Read\w*|Exists|Get\w*",
    "io.directory": r"Exists|Get\w*|Enumerate\w*",
    "io.path": r"(?!GetTempFileName$)\w+",
    "environment": r"(?!Exit$|FailFast$)\w+",
}
PS_WRITE_METHOD = re.compile(r"(?:\.|::)(?:Delete|Create\w*|CopyTo|MoveTo|Move|Copy|Save\w*|Write\w*|Set\w*|Kill|Start|"
                             r"Invoke\w*|Execute\w*|Open\w*|Append\w*|Remove\w*|Clear|Flush|Install\w*|Uninstall\w*)\s*\(", re.I)
PS_LEXEME = re.compile(r"""(?P<s>@'.*?'@|@".*?"@|'(?:[^']|'')*'|"(?:[^"`]|`.|"")*")|(?P<c><\#.*?\#>|\#[^\n]*)""", re.S)
PS_SPLIT = re.compile(r"&&|\|\||[;|{}()\n]")


def env_path(name):
    """环境变量的值；HOME / USERPROFILE 没设时按家目录算"""
    val = os.environ.get(name) or os.environ.get(name.upper())
    if not val and name.upper() in ("HOME", "USERPROFILE"):
        val = HOME
    return val or ""


def on_protected_drive(path):
    """路径在受保护的盘上：C:\\x、C:/x、Git Bash 的 /c/x、WSL 的 /mnt/c/x，以及家目录在受保护盘上时的 ~"""
    p = (path or "").strip().strip("\"'").replace("\\", "/")
    if p.startswith("~"):
        p = HOME.replace("\\", "/")
    m = re.match(r"([A-Za-z]):(?:/|$)", p) or re.match(r"/(?:mnt/)?([A-Za-z])(?:/|$)", p)
    return bool(m) and m.group(1).upper() in PROTECTED_DRIVES


def touches_protected(text, cwd):
    """当前目录在受保护的盘上，或者文字里出现了指向它的路径、环境变量、~、Git Bash 的 /tmp。
    返回第一个命中的写法（写进日志），没有返回 None"""
    if on_protected_drive(cwd):
        return "当前目录 %s" % cwd
    for m in DRIVE_REF.finditer(text):
        if (m.group(1) or m.group(2)).upper() in PROTECTED_DRIVES:
            return m.group(0).lstrip(" \t\r\n'\"=(,;")
    m = re.search(r"file:///([A-Za-z])(?::|%3a)", text, re.I)
    if m and m.group(1).upper() in PROTECTED_DRIVES:
        return m.group(0)
    for m in ENV_REF.finditer(text):
        name = m.group(1) or m.group(2)
        if name.lower() in PS_AUTOMATIC and not m.group(0).lower().startswith(("$env:", "${env:")):
            continue
        if on_protected_drive(env_path(name)):
            return m.group(0)
    if re.search(r"""(?:^|[\s'"=])~(?=[\\/\s'"]|$)""", text) and on_protected_drive(HOME):
        return "~"
    if (PLATFORM == "windows" and re.search(r"""(?<![\w./-])/(?:var/)?tmp(?=/|[\s'"`;|&)]|$)""", text)
            and on_protected_drive(env_path("TEMP") or env_path("TMP"))):
        return "/tmp"                                   # Git Bash 的 /tmp 就是 %TEMP%，在 C 盘上
    return None


def writes_file(segment):
    """这一段命令有没有用 > / >> 往文件里写（写到 /dev/null、NUL、$null 不算）"""
    # 引号里的内容换成占位符而不是删掉：> "C:/x.txt" 这种写到带引号路径的，也得认出是在写文件
    bare = re.sub(r"""'[^']*'|"(?:\\.|[^"\\])*\"""", "Q", segment)
    return any(m.group(1).lower() not in ("/dev/null", "nul", "nul:", "$null") for m in REDIRECT.finditer(bare))


def shell_word(word):
    """一个 shell 词真正的值：去掉引号，处理双引号里 \\$ \\" 这类转义（bash -c "…" 里真正要执行的命令）"""
    out, i, n, q = [], 0, len(word), None
    while i < n:
        c = word[i]
        if q == "'":
            if c == "'":
                q = None
            else:
                out.append(c)
        elif q == '"':
            if c == '"':
                q = None
            elif c == "\\" and i + 1 < n and word[i + 1] in '$`"\\\n':
                i += 1
                out.append(word[i])
            else:
                out.append(c)
        elif c in "'\"":
            q = c
        elif c == "\\" and i + 1 < n:
            i += 1
            out.append(word[i])
        else:
            out.append(c)
        i += 1
    return "".join(out)


def ps_opt(arg, name, shortest):
    """PowerShell 参数名可以缩写：-NoP 就是 -NoProfile，-ex 就是 -ExecutionPolicy"""
    return len(arg) >= shortest and name.startswith(arg)


def wrapped_command(name, args):
    """bash -c '…'、powershell -Command "…"、cmd /c … 里真正要执行的命令。
    不是这类写法返回 None；是但看不到要执行什么（-File 脚本、-EncodedCommand、bash 脚本.sh）返回 ''"""
    if name in SHELL_WRAPPERS:
        for i, a in enumerate(args):
            if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", a):
                return args[i + 1] if i + 1 < len(args) else ""
            if not a.startswith("-"):
                return ""                               # bash 脚本.sh：跑的是脚本文件
        return ""
    if name in PS_WRAPPERS:
        i = 0
        while i < len(args):
            a = args[i].lower()
            if ps_opt(a, "-command", 2):
                rest = args[i + 1:]
                return "" if rest[:1] == ["-"] else " ".join(rest)   # -Command -：从标准输入读，看不到
            if a in ("-e", "-ec") or ps_opt(a, "-encodedcommand", 3) or ps_opt(a, "-file", 2):
                return ""
            if (ps_opt(a, "-executionpolicy", 3) or ps_opt(a, "-windowstyle", 2) or ps_opt(a, "-configurationname", 4)
                    or ps_opt(a, "-workingdirectory", 3) or a in ("-wd", "-ep", "-of", "-if")
                    or ps_opt(a, "-outputformat", 2) or ps_opt(a, "-inputformat", 3) or ps_opt(a, "-version", 2)
                    or ps_opt(a, "-settingsfile", 3) or ps_opt(a, "-psconsolefile", 3)):
                i += 2                                  # 带值的参数，连值一起跳过
                continue
            if a.startswith("-"):
                i += 1                                  # -NoProfile、-NonInteractive 这类开关
                continue
            return " ".join(args[i:])                   # 没写 -Command：剩下的就是命令
        return ""
    if name == "cmd":
        for i, a in enumerate(args):
            if a.lower() in ("/c", "/k", "/r"):
                return " ".join(args[i + 1:])
        return ""
    return None


def ps_read_only(text, depth=0):
    """一段 PowerShell 是不是只在读。字符串和注释先遮住（长度不变），再看：重定向、.NET 写操作、
    全文所有 动词-名词 命令（包括花括号里的）、每条语句开头是什么"""
    if any(m.group("s") and m.group("s")[0] in '"@' and "$(" in m.group("s") for m in PS_LEXEME.finditer(text)):
        return False                                    # "…$(…)…" 会执行里面的命令
    masked = PS_LEXEME.sub(lambda m: (m.group(0)[0] + "_" * (len(m.group(0)) - 2) + m.group(0)[-1]) if m.group("s")
                           else re.sub(r"[^\n]", " ", m.group(0)), text)
    if ">" in re.sub(r"[\d*]?>&\d|[\d*]?>{1,2}\s*\$null", "", masked):
        return False                                    # > 文件
    if PS_WRITE_METHOD.search(masked):
        return False
    for m in re.finditer(r"\[([\w.]+)\]\s*::\s*(\w*)", masked):
        name = m.group(1).lower()
        name = name[7:] if name.startswith("system.") else name
        if name in PS_SAFE_TYPES:
            continue
        if name in PS_READ_STATIC and re.fullmatch(PS_READ_STATIC[name], m.group(2), re.I):
            continue                                    # [IO.File]::ReadAllText 这类读取
        return False
    for m in re.finditer(r"(?<![\w$.-])([A-Za-z]+)-([A-Za-z]\w*)", masked):
        if m.group(1).lower() not in PS_READ_VERBS and m.group(0).lower() not in PS_READ_CMDLETS:
            return False                                # 花括号里藏着的 Remove-Item 之类也算
    starts = [0] + [m.end() for m in PS_SPLIT.finditer(masked)]
    ends = [m.start() for m in PS_SPLIT.finditer(masked)] + [len(masked)]
    for a, b in zip(starts, ends):
        s = masked[a:b].strip()
        if not s:
            continue
        if s[0] == "&" or re.match(r"\.\s", s):         # & 程序、. 脚本：执行别的东西
            return False
        if s[0] in "$'\"@[0123456789-+!,.":
            if re.match(r"\$[\w:]+(?:\.\w+|\[[^\]]*\])+\s*[-+*/]?=(?!=)", s):
                return False                            # $_.Attributes = …：给对象的属性赋值，可能改到文件
            continue
        m = re.match(r"[^\s=]+", s)
        if not m:
            return False
        first = re.sub(r"(?:\.exe)?$", "", m.group(0).lower())
        if re.match(r"[A-Za-z]+-[A-Za-z]", first) or re.match(r"\w+\s*=(?!=)", s):
            continue                                    # 命令已在上面查过；n='…' 是哈希表的键
        if first in PS_KEYWORDS or first in PS_READ_ALIASES:
            continue
        if read_only(text[a:b].strip(), False, depth):  # git status、ipconfig、powershell -Command … 这类
            continue
        return False
    return True


def sed_script_safe(script):
    """一段 sed 脚本里没有会写文件、执行命令的部分：w / W / e 命令，s/// 的 e、w 标志。
    按 sed 的结构走一遍（地址 → 命令 → 参数），而不是看有没有这几个字母：s/hello/world/ 里的 e、w 只是文字"""
    s, i, n = script, 0, len(script)
    while i < n:
        c = s[i]
        if c in " \t\r\n;{}!,+~" or c.isdigit() or c == "$":
            i += 1                                      # 分隔、分组、取反、地址里的数字 / $ / first~step / addr,+N
            continue
        if c == "/" or c == "\\":                       # 地址 /正则/ 或 \c正则c
            delim = "/" if c == "/" else (s[i + 1] if i + 1 < n else "")
            j = i + (1 if c == "/" else 2)
            while j < n and s[j] != delim:
                j += 2 if s[j] == "\\" else 1
            i = j + 1
            while i < n and s[i] in "IM":
                i += 1
            continue
        if c in "ewW":
            return False                                # e 执行命令、w / W 写文件
        if c in "sy":                                   # s/正则/替换/标志、y/源/目标/
            d = s[i + 1] if i + 1 < n else ""
            if not d or d in "\n\\":
                return False
            j, parts = i + 2, 0
            while j < n and parts < 2:
                if s[j] == "\\":
                    j += 2
                    continue
                parts += s[j] == d
                j += 1
            while c == "s" and j < n and s[j] not in ";\n}":
                if s[j] in "ew":
                    return False                        # s///e 执行替换结果、s///w 写文件
                j += 1
            i = j
            continue
        if c in "aic":                                  # a / i / c 后面到行尾都是文字
            nl = s.find("\n", i)
            i = n if nl < 0 else nl + 1
            continue
        if c in "rRbtTvlLqQ:#":                          # 读文件、跳转、标签、注释：参数到 ; 或行尾
            j = i + 1
            while j < n and s[j] not in ("\n" if c in "rR#" else ";\n}"):
                j += 1
            i = j
            continue
        i += 1                                          # p d n N P D h H g G x = l z F 这类单字母命令
    return True


def sed_safe(args):
    """sed 的参数里取出脚本（-e 后面的，或者第一个不是选项的参数）逐段检查。
    带 --sandbox 时 sed 自己会拒绝 e / w / r，算安全；脚本从 -f 文件读的看不到，算不安全"""
    if "--sandbox" in args:
        return True
    scripts, positional, i = [], [], 0
    while i < len(args):
        a = args[i]
        if a in ("-e", "--expression") or re.fullmatch(r"-[a-zA-Z]*e", a):
            scripts.append(args[i + 1] if i + 1 < len(args) else "")
            i += 2
            continue
        if a.startswith("--expression="):
            scripts.append(a.split("=", 1)[1])
        elif a in ("-f", "--file") or a.startswith("--file=") or re.fullmatch(r"-[a-zA-Z]*f\S*", a):
            return False
        elif a in ("-l", "--line-length"):
            i += 1
        elif not a.startswith("-"):
            positional.append(a)
        i += 1
    if not scripts and positional:
        scripts = positional[:1]
    return all(sed_script_safe(x) for x in scripts)


def read_only(cmd, powershell=False, depth=0):
    """整条命令只是在读：每一段都是只读命令，没有往文件里写，也没有 $(...) 和反引号。
    套了 bash -c / powershell -Command / cmd /c 的，按里面真正执行的命令判断（最多拆两层）；PowerShell 按它自己的语法判断"""
    if powershell:
        return ps_read_only(cmd, depth)
    cmd = re.sub(r"[\\`]\r?\n", " ", cmd)
    if "$(" in cmd or "`" in cmd:
        return False
    cmd = re.sub(r"\d*>&\d+", " ", cmd)            # 2>&1 只是合并输出，别让 & 把命令切开
    cmd = re.sub(r"&(>>?)", r"\1", cmd)             # &>file 和 >file 一样是写文件
    for raw, words in split_segments(cmd):
        if writes_file(raw):
            return False
        plain = [unquote(w) for w in words]
        while plain and ENV_ASSIGN.fullmatch(plain[0]):
            plain.pop(0)
            words = words[1:]
        if not plain:
            continue
        name = re.sub(r"(?:\.exe)?$", "", re.sub(r"^.*[\\/]", "", plain[0]).lower())
        inner = wrapped_command(name, [shell_word(w) for w in words[1:]])
        if inner is not None:
            if not inner.strip() or depth >= 2 or not read_only(inner, name in PS_WRAPPERS, depth + 1):
                return False
            continue
        if not READ_ONLY.match(" ".join(plain)):
            return False
        if name == "sed" and not sed_safe(plain[1:]):
            return False
    return True


def protected_write(tool, tool_input, cwd):
    """Windows（含 WSL）上涉及受保护的盘、又不只是读的，返回原因；否则返回 None"""
    if PLATFORM not in ("windows", "wsl") or not PROTECTED_DRIVES:
        return None
    reason = "Windows：涉及 %s 盘「%%s」，除了读都要你确认" % "/".join(sorted(PROTECTED_DRIVES))
    if tool in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        relative = not re.match(r"[A-Za-z]:|[\\/~]", path)
        return reason % path if on_protected_drive(path) or (relative and on_protected_drive(cwd)) else None
    if tool in SHELL_TOOLS:
        cmd = tool_input.get("command") if isinstance(tool_input.get("command"), str) else ""
        hit = touches_protected(cmd, cwd)
        if not hit and on_protected_drive(HOME):        # pip install、npm -g 这类：不提路径，也写进 C 盘的用户目录
            m = next((m for m in (re.search(p, cmd, re.I) for p in USER_PROFILE_WRITERS) if m), None)
            hit = m and re.sub(r"\s+", " ", m.group(0)).strip()
        return reason % hit if hit and not read_only(cmd, tool == "PowerShell") else None
    if tool.startswith("mcp__"):
        hit = touches_protected("\n".join(strings(tool_input)), "")
        return reason % hit if hit else None
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


def decide(tool, tool_input, permission_mode="", cwd=""):
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
    reason = protected_write(tool, tool_input, cwd)
    if reason:
        return False, reason
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
    cwd = event.get("cwd") or ""
    if event.get("hook_event_name") == "PreToolUse":
        # 执行前检查（只在 Windows 上登记）：涉及 C 盘的写操作强制弹窗，acceptEdits 模式下也一样
        reason = protected_write(tool, tool_input, cwd)
        if reason:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "permissionDecision": "ask",
                                                     "permissionDecisionReason": reason}}))   # 只输出 ASCII：Windows 控制台编码打不了中文
            log("要求确认（%s）%s" % (reason, describe(event, tool, tool_input)))
        return
    ok, why = decide(tool, tool_input, event.get("permission_mode") or "", cwd)
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
