#!/usr/bin/env python3
"""安装 / 更新 / 卸载 claude-code-auto-approve（Linux / macOS / Windows 通用，只用标准库）。

  python3 install.py                 安装或更新。第一次装默认 dryrun（只记日志不放行），验证过再开
  python3 install.py --mode on       装好并直接开启（已经装过时，也可以只用来切开关）
  python3 install.py --uninstall     卸载：从 settings.json 摘掉这个钩子、删掉钩子脚本；日志和开关文件留着

Windows 上把 python3 换成 python（或 py -3）。

安装做的事：
  1. 跑一遍 tests/test_hook.py（--skip-tests 跳过）
  2. 把 auto-approve.py 拷到 <配置目录>/hooks/（内容没变就不动；装着的那份被改过，先备份再覆盖）
  3. 写开关文件（第一次装是 dryrun）
  4. 在 <配置目录>/settings.json 的 hooks.PermissionRequest 里登记这个钩子。
     原有的配置、别的钩子都不动；有改动才写，写之前先备份成 settings.json.bak-<时间>；重复安装不会重复登记
  5. 把登记的命令按 Claude Code 的方式实际跑一遍，确认能放行、能拦下（自检）
<配置目录> 默认是 $CLAUDE_CONFIG_DIR，没设就是 ~/.claude；可以用 --config-dir 指定。
"""
import argparse
import filecmp
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
HOOK_NAME = "auto-approve.py"
MATCHER = "Bash|PowerShell|Edit|MultiEdit|NotebookEdit|Write|Read|Glob|Grep|WebFetch|WebSearch|Skill|Workflow|mcp__.*"
TIMEOUT = 10
WINDOWS = os.name == "nt"
LEGACY = re.compile(r"[\\/]\.claude[\\/]hooks[\\/]auto-approve\.py\b")   # 手动登记（没用 install.py）的：<某处>/.claude/hooks/auto-approve.py

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")   # 输出被重定向到不支持中文的编码时，别因为打印崩掉


def fail(msg):
    print("\n✗ " + msg)
    sys.exit(1)


def config_dir(arg):
    if arg is not None and not arg.strip():
        fail("--config-dir 不能是空的。")
    return os.path.abspath(os.path.expanduser(arg or os.environ.get("CLAUDE_CONFIG_DIR") or "~/.claude"))


def stamp(path):
    """不会撞名的备份文件名：同一秒里装两次，也不会把最早那份备份盖掉"""
    base = "%s.bak-%s" % (path, time.strftime("%Y%m%d-%H%M%S"))
    name, i = base, 1
    while os.path.exists(name):
        i += 1
        name = "%s-%d" % (base, i)
    return name


def read_mode(path):
    """和钩子同样的读法：认 UTF-16（PowerShell 5 写出来的）和 BOM，认不出的按 off"""
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return "on"
    except OSError:
        return "off"
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig", "replace")
    mode = text.strip().lower()
    return mode if mode in ("on", "off", "dryrun") else "off"


# ---------------------------- settings.json ----------------------------

def load_settings(path):
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8-sig") as f:
            text = f.read()
        data = json.loads(text) if text.strip() else {}
    except UnicodeDecodeError:
        fail("%s 不是 UTF-8 编码（可能是用 PowerShell 5 的 > 或记事本另存为 ANSI 写出来的）。"
             "没有改动任何东西，请用 UTF-8 重新保存后再装。" % path)
    except ValueError as e:
        fail("%s 不是合法的 JSON（%s）。没有改动任何东西，修好再装。" % (path, e))
    except OSError as e:
        fail("读不了 %s：%s" % (path, e))
    if not isinstance(data, dict):
        fail("%s 的最外层不是一个对象 {...}。没有改动任何东西。" % path)
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict) or not isinstance(hooks.get("PermissionRequest", []), list):
        fail("%s 里的 hooks / hooks.PermissionRequest 格式不对。没有改动任何东西，请手动检查。" % path)
    return data


def save_settings(path, data, before):
    """内容没变就什么都不做。变了：先备份，再写临时文件、整体替换（写一半断电也不会留下半个文件）。
    软链接写到它指向的文件。返回备份文件名（没写就返回 None）"""
    if data == before:
        return None
    target = os.path.realpath(path)
    backup, tmp = None, None
    try:
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if os.path.exists(target):
            backup = stamp(path)
            shutil.copy2(target, backup)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(target), prefix=".settings.", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        if os.path.exists(target):
            shutil.copymode(target, tmp)
        os.replace(tmp, target)
        tmp = None
    except OSError as e:
        fail("写不了 %s：%s" % (path, e))
    finally:
        if tmp and os.path.exists(tmp):
            os.remove(tmp)
    return backup or "(新建)"


def same_path(a, b):
    a, b = a.replace("\\", "/"), b.replace("\\", "/")
    return a.lower() == b.lower() if WINDOWS else a == b


def is_ours(command, dst):
    """settings.json 里的这条命令是不是这个钩子：指向本次安装的位置，或者手动装在 .claude/hooks/ 下的"""
    try:
        words = shlex.split(command, posix=not WINDOWS)
    except ValueError:
        words = command.split()
    words = [w.strip("\"'") for w in words]
    return any(same_path(w, dst) or LEGACY.search(w) and w.replace("\\", "/").endswith("/" + HOOK_NAME) for w in words)


def hook_entries(data):
    entries = data.get("hooks", {}).get("PermissionRequest", [])
    return [e for e in entries if isinstance(e, dict) and isinstance(e.get("hooks"), list)]


def our_commands(data, dst):
    return [h.get("command") for e in hook_entries(data) for h in e["hooks"]
            if isinstance(h, dict) and is_ours(str(h.get("command", "")), dst)]


def strip_ours(entries, dst):
    """去掉 PermissionRequest 里属于这个钩子的项，别人的原样留着（本来就是空列表的也留着）"""
    kept = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
            kept.append(entry)
            continue
        hooks = [h for h in entry["hooks"] if not (isinstance(h, dict) and is_ours(str(h.get("command", "")), dst))]
        if hooks or not entry["hooks"]:
            kept.append(dict(entry, hooks=hooks) if len(hooks) != len(entry["hooks"]) else entry)
    return kept


# ---------------------------- 钩子命令 ----------------------------

def python_command(override):
    """settings.json 里用哪个 Python 跑钩子：挑一个真能跑、版本够的；优先用命令名，不绑死安装位置"""
    if override:
        if WINDOWS:
            override = override.replace("\\", "/")      # 反斜杠到了 Git Bash 里会被吃掉
            if " " in override and not override.startswith(('"', "py ")) and os.path.exists(override):
                override = '"%s"' % override
        return override
    candidates = [["python"], ["py", "-3"], ["python3"]] if WINDOWS else [["python3"], ["python"]]
    for cand in candidates:
        exe = shutil.which(cand[0])
        if not exe:
            continue
        try:   # 跑 PATH 上找到的那一个（Windows 上没装 Python 时，python 是个应用商店的占位程序）
            out = subprocess.run([exe] + cand[1:] + ["-c", "import sys; print(sys.version_info >= (3, 8))"],
                                 stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30)
        except (OSError, subprocess.SubprocessError):
            continue
        if out.stdout.decode(errors="replace").strip() == "True":
            return " ".join(cand)
    exe = sys.executable.replace("\\", "/") if WINDOWS else sys.executable
    if " " in exe:
        print("⚠ 找不到可用的 python 命令，只能用完整路径 %s；路径里有空格，在 PowerShell 下跑不起来。"
              "建议装 Python 时勾上 Add to PATH，或者用 --python 指定" % exe)
        return '"%s"' % exe
    return exe


def git_bash():
    """按 Claude Code 的找法找 Git Bash：CLAUDE_CODE_GIT_BASH_PATH、PATH 上 git 旁边的、默认安装位置"""
    cands = []
    env = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if env and os.path.basename(env).lower() in ("bash.exe", "bash"):
        cands.append(env)
    git = shutil.which("git")
    if git:
        cands.append(os.path.join(os.path.dirname(os.path.dirname(git)), "bin", "bash.exe"))
    cands += [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"]
    return next((p for p in cands if os.path.isfile(p)), None)


def use_powershell():
    """Windows 上找不到 Git Bash 时，Claude Code 用 PowerShell 跑钩子"""
    return WINDOWS and not git_bash()


def hook_entry(python, hook_path, powershell):
    """settings.json 里登记的那一项"""
    if WINDOWS:
        command = '%s "%s"' % (python, hook_path.replace("\\", "/"))   # 正斜杠 + 双引号：Git Bash、PowerShell 都认
        if powershell and python.startswith('"'):
            command = "& " + command                                    # PowerShell 里带引号的程序路径要加 &
    else:
        command = "%s %s" % (python, shlex.quote(hook_path))
    hook = {"type": "command", "command": command, "timeout": TIMEOUT}
    if powershell:
        hook["shell"] = "powershell"   # 不写的话，没有 Git Bash 时 Claude Code 只认退出码 0 和 2，判定会出错
    return {"matcher": MATCHER, "hooks": [hook]}


def shells():
    """Claude Code 跑钩子命令用的 shell。返回 [(名字, 调用方式)]。
    POSIX 上是 /bin/sh；Windows 上优先 Git Bash，没有就用 PowerShell"""
    if not WINDOWS:
        return [("sh", lambda c: ["/bin/sh", "-c", c])]
    bash = git_bash()
    if bash:
        return [("Git Bash", lambda c: [bash, "-c", c])]
    ps = shutil.which("pwsh") or shutil.which("powershell") or "powershell"
    return [("PowerShell", lambda c: [ps, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", c])]


def self_check(command):
    """把登记的命令用 Claude Code 会用的 shell 跑一遍，喂两个事件：普通命令应放行，危险命令应留给人。返回出错信息列表"""
    errors = []
    tmp = tempfile.mkdtemp(prefix="auto-approve-check-")
    mode = os.path.join(tmp, "mode")
    with open(mode, "w") as f:
        f.write("on\n")
    env = dict(os.environ, AUTO_APPROVE_MODE_FILE=mode, AUTO_APPROVE_LOG=os.path.join(tmp, "log"))
    cases = [("ls -la", True), ("rm -rf ~", False)]   # 只是喂给钩子判断的文字，不会被执行
    try:
        for name, argv in shells():
            for cmd, expect in cases:
                event = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}).encode()
                try:
                    p = subprocess.run(argv(command), input=event, env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=TIMEOUT * 3)
                    out, err = p.stdout.decode(errors="replace"), p.stderr.decode(errors="replace")
                except (OSError, subprocess.SubprocessError) as e:
                    out, err = "", str(e)
                if ('"allow"' in out) != expect:
                    errors.append("[%s] 喂「%s」应%s，实际%s。%s" % (
                        name, cmd, "放行" if expect else "留给人", "放行" if '"allow"' in out else "没有放行",
                        err.strip()[-300:]))
                    break
            else:
                print("  ✓ 自检通过（%s）" % name)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return errors


def copy_hook(src, dst):
    """内容一样就不动；装着的那份被手动改过，先备份再覆盖；软链接换成普通文件"""
    if os.path.islink(dst):
        print("  - %s 原来是个软链接，换成普通文件（链接指向的文件不动）" % dst)
        os.remove(dst)
    elif os.path.exists(dst):
        if os.path.samefile(src, dst) or filecmp.cmp(src, dst, shallow=False):
            return "没有变化"
        backup = stamp(dst)
        shutil.copy2(dst, backup)
        print("  - 装着的那份和这次的不一样，旧的备份为 %s" % os.path.basename(backup))
    shutil.copyfile(src, dst)
    if not WINDOWS:
        os.chmod(dst, 0o755)
    return "已更新"


# ---------------------------- 安装 / 卸载 ----------------------------

def install(args):
    cfg = config_dir(args.config_dir)
    hooks_dir = os.path.join(cfg, "hooks")
    settings_path = os.path.join(cfg, "settings.json")
    mode_path = os.path.join(cfg, "auto-approve.mode")
    src = os.path.join(HERE, HOOK_NAME)
    dst = os.path.join(hooks_dir, HOOK_NAME)
    print("配置目录：%s" % cfg)

    # 已经装好、只想切开关：只改开关文件，不跑用例、不重装
    if args.mode and not args.python and os.path.isfile(dst) and our_commands(load_settings(settings_path), dst):
        write_mode(mode_path, args.mode)
        print("  ✓ 开关：%s → %s" % (mode_path, read_mode(mode_path)))
        next_steps(read_mode(mode_path), cfg, args)
        return

    if not args.skip_tests:
        test = os.path.join(HERE, "tests", "test_hook.py")
        if not os.path.isfile(test):
            fail("找不到 %s。请下载完整的仓库，或者加 --skip-tests 跳过用例。" % test)
        p = subprocess.run([sys.executable, test], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        out = p.stdout.decode("utf-8", errors="replace").strip()
        if p.returncode != 0:
            print(out)
            fail("钩子的用例没通过，没有安装。")
        print("  ✓ 用例：%s" % out.splitlines()[-1])

    before = load_settings(settings_path)   # 先读，格式不对就在动任何文件之前停下
    data = json.loads(json.dumps(before))
    try:
        os.makedirs(hooks_dir, exist_ok=True)
        print("  ✓ 钩子脚本：%s（%s）" % (dst, copy_hook(src, dst)))
        # 开关要在登记之前写好：登记完立刻生效，开关文件不存在等于 on
        if args.mode or not os.path.exists(mode_path):
            write_mode(mode_path, args.mode or "dryrun")
    except OSError as e:
        fail("写不了 %s：%s" % (hooks_dir, e))
    mode = read_mode(mode_path)

    entry = hook_entry(python_command(args.python), dst, use_powershell())
    command = entry["hooks"][0]["command"]
    old = [c for c in our_commands(data, dst) if c != command]
    hooks = data.setdefault("hooks", {})
    entries = strip_ours(hooks.get("PermissionRequest", []), dst)
    entries.append(entry)
    hooks["PermissionRequest"] = entries
    backup = save_settings(settings_path, data, before)
    for c in old:
        print("  - 换掉了原来登记的：%s" % c)
    if backup:
        print("  ✓ 已登记到 %s（%s）" % (settings_path, backup if backup == "(新建)" else "原文件备份为 " + os.path.basename(backup)))
    else:
        print("  ✓ %s 里已经登记过，没有改动" % settings_path)
    print("    命令：%s" % command)
    print("  ✓ 开关：%s → %s" % (mode_path, mode))

    errors = self_check(command)
    for msg in errors:
        print("  ✗ " + msg)
    if errors:
        hint = ("常见原因是 Claude Code 的进程找不到 python，可以用 --python 指定，比如 --python \"py -3\"。" if WINDOWS
                else "常见原因是 python3 不在 PATH 里，可以用 --python 指定 Python 的完整路径。")
        fail("装好了，但自检没过：Claude Code 很可能跑不起这个钩子。先运行 install.py --uninstall 撤掉，"
             "否则 Claude Code 可能把需要确认的操作直接拒绝。" + hint)
    next_steps(mode, cfg, args)


def write_mode(path, value):
    with open(path, "w", encoding="utf-8") as f:
        f.write(value + "\n")


def next_steps(mode, cfg, args):
    launcher = (python_command(None) if WINDOWS else "python3").strip('"')
    if " " in launcher and not launcher.startswith("py "):
        launcher = '"%s"' % launcher
    extra = ' --config-dir "%s"' % cfg if args.config_dir else ""
    print()
    if mode == "dryrun":
        print("现在是试运行：只记日志、不放行。在 Claude Code 里触发一次授权弹窗（照常点掉），然后看日志：")
        print("  %s" % os.path.join(cfg, "hooks", "auto-approve.log"))
        print("出现带会话目录的「[试运行] 会自动确认」那一行，就说明钩子在工作了，再开启：")
        print("  %s install.py --mode on%s" % (launcher, extra))
    elif mode == "off":
        print("现在是暂停状态，钩子什么都不做，弹窗照常出现。要开启：%s install.py --mode on%s" % (launcher, extra))
    else:
        print("已开启。要暂停：%s install.py --mode off%s" % (launcher, extra))
    print("开关改完立刻生效，不用重启 Claude Code。")


def uninstall(args):
    cfg = config_dir(args.config_dir)
    settings_path = os.path.join(cfg, "settings.json")
    dst = os.path.join(cfg, "hooks", HOOK_NAME)
    print("配置目录：%s" % cfg)
    before = load_settings(settings_path)
    data = json.loads(json.dumps(before))
    removed = our_commands(data, dst)
    if removed:
        hooks = data["hooks"]
        entries = strip_ours(hooks.get("PermissionRequest", []), dst)
        if entries:
            hooks["PermissionRequest"] = entries
        else:
            hooks.pop("PermissionRequest", None)
        if not hooks:
            data.pop("hooks", None)
        backup = save_settings(settings_path, data, before)
        for c in removed:
            print("  ✓ 已从 settings.json 摘掉：%s" % c)
        print("    原文件备份为 %s" % os.path.basename(backup))
    else:
        print("  - settings.json 里没有登记这个钩子")
    if os.path.lexists(dst):
        os.remove(dst)
        print("  ✓ 已删除 %s" % dst)
    print("开关文件和日志留着没删：%s、%s" % (os.path.join(cfg, "auto-approve.mode"),
                                       os.path.join(cfg, "hooks", "auto-approve.log")))


def main():
    ap = argparse.ArgumentParser(description="安装 / 卸载 Claude Code 授权弹窗自动确认钩子")
    ap.add_argument("--mode", choices=["on", "off", "dryrun"], help="同时把开关设成这个值（默认：第一次装是 dryrun，之后不动）")
    ap.add_argument("--uninstall", action="store_true", help="卸载")
    ap.add_argument("--config-dir", help="Claude Code 配置目录（默认 $CLAUDE_CONFIG_DIR 或 ~/.claude）")
    ap.add_argument("--python", help="settings.json 里用来跑钩子的 Python 命令（默认自动挑，如 python3 / python / py -3）")
    ap.add_argument("--skip-tests", action="store_true", help="安装前不跑用例")
    args = ap.parse_args()
    if sys.version_info < (3, 8):
        fail("需要 Python 3.8 或更高版本，当前是 %s" % sys.version.split()[0])
    (uninstall if args.uninstall else install)(args)


if __name__ == "__main__":
    main()
