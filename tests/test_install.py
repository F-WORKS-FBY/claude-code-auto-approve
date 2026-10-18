#!/usr/bin/env python3
"""install.py 的用例。跑法：python3 tests/test_install.py（Windows 上用 python）

每个用例用一个全新的临时配置目录（--config-dir），不碰你真实的 ~/.claude。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTALL = os.path.join(ROOT, "install.py")
sys.path.insert(0, ROOT)
MATCHER = __import__("install").MATCHER
WINDOWS = os.name == "nt"
TMP = tempfile.mkdtemp(prefix="auto-approve-install-test-")
GUARD = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/x/other-guard.py"}]}
OTHER = {"matcher": "Read", "hooks": [{"type": "command", "command": "/x/someone-elses-hook.sh"}]}


def fresh(name):
    cfg = os.path.join(TMP, name)
    os.makedirs(cfg)
    return cfg


def install(cfg, *args, env_extra=None):
    env = dict(os.environ, PYTHONIOENCODING="utf-8", AUTO_APPROVE_PLATFORM="posix")   # 默认按 Linux / macOS 装
    env.update(env_extra or {})
    env.pop("CLAUDE_CONFIG_DIR", None)
    p = subprocess.run([sys.executable, INSTALL, "--config-dir", cfg, "--skip-tests"] + list(args),
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    return p.returncode, p.stdout.decode("utf-8", errors="replace")


def write(path, data):
    with open(path, "wb") as f:
        f.write(data if isinstance(data, bytes) else json.dumps(data).encode())


def read(path):
    with open(path, "rb") as f:
        return f.read()


def settings(cfg):
    with open(os.path.join(cfg, "settings.json"), encoding="utf-8-sig") as f:
        return json.load(f)


def ours(data):
    return [h["command"] for e in data.get("hooks", {}).get("PermissionRequest", []) if isinstance(e, dict)
            for h in (e.get("hooks") or []) if "hooks" + os.sep + "auto-approve.py" in h.get("command", "").replace("/", os.sep)
            and "/opt/" not in h.get("command", "")]


def mode(cfg):
    with open(os.path.join(cfg, "auto-approve.mode")) as f:
        return f.read().strip()


def backups(cfg):
    return sorted(f for f in os.listdir(cfg) if f.startswith("settings.json.bak-"))


def run_registered(cfg, command_text, override_mode=True):
    """按 Claude Code 的方式（经过 shell）跑 settings.json 里登记的命令，返回是否放行。
    override_mode=False 时不指定开关文件，看钩子自己找到的开关（HOME 指向一个空目录，免得读到真实的 ~/.claude）"""
    env = dict(os.environ)
    env.pop("CLAUDE_CONFIG_DIR", None)
    fake_home = os.path.join(cfg, "fake-home")
    os.makedirs(fake_home, exist_ok=True)
    env.update(HOME=fake_home, USERPROFILE=fake_home)
    if override_mode:
        write(os.path.join(cfg, "check-mode"), b"on\n")
        env.update(AUTO_APPROVE_MODE_FILE=os.path.join(cfg, "check-mode"), AUTO_APPROVE_LOG=os.path.join(cfg, "check-log"))
    event = json.dumps({"tool_name": "Bash", "tool_input": {"command": command_text}}).encode()
    p = subprocess.run(ours(settings(cfg))[0], shell=True, input=event, env=env,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return '"allow"' in p.stdout.decode(errors="replace")


def case_fresh():
    cfg = fresh("fresh")
    code, out = install(cfg)
    data = settings(cfg)
    entry = data["hooks"]["PermissionRequest"][0]
    return (code == 0 and len(ours(data)) == 1 and entry["matcher"] == MATCHER and "PreToolUse" not in data["hooks"]
            and os.path.isfile(os.path.join(cfg, "hooks", "auto-approve.py")) and mode(cfg) == "dryrun"
            and "自检通过" in out and "试运行" in out), out


def pre_ours(data):
    return [h["command"] for e in data.get("hooks", {}).get("PreToolUse", [])
            for h in e.get("hooks", []) if "auto-approve.py" in h.get("command", "")]


def case_c_drive_guard(plat):
    cfg = fresh("c-drive-" + plat)
    write(os.path.join(cfg, "settings.json"), {"hooks": {"PreToolUse": [GUARD]}})
    code, out = install(cfg, env_extra={"AUTO_APPROVE_PLATFORM": plat})
    data = settings(cfg)
    pre = data["hooks"]["PreToolUse"]
    registered = (code == 0 and pre[0] == GUARD and len(pre_ours(data)) == 1 and pre[1]["matcher"] == __import__("install").PRE_MATCHER
                  and "执行前检查" in out and "自检通过" in out)
    code2, out2 = install(cfg, "--uninstall", env_extra={"AUTO_APPROVE_PLATFORM": plat})
    data = settings(cfg)
    removed = code2 == 0 and data["hooks"] == {"PreToolUse": [GUARD]}
    return registered and removed, out + out2


def case_keep_existing():
    cfg = fresh("keep")
    write(os.path.join(cfg, "settings.json"), {"env": {"A": "1"}, "model": "opus",
                                               "hooks": {"PreToolUse": [GUARD], "PermissionRequest": [OTHER]}})
    code, out = install(cfg)
    data = settings(cfg)
    return (code == 0 and data["env"] == {"A": "1"} and data["model"] == "opus"
            and data["hooks"]["PreToolUse"] == [GUARD] and data["hooks"]["PermissionRequest"][0] == OTHER
            and len(ours(data)) == 1 and len(backups(cfg)) == 1), out


def case_reinstall():
    cfg = fresh("reinstall")
    write(os.path.join(cfg, "settings.json"), {"theme": "dark"})
    install(cfg)
    write(os.path.join(cfg, "auto-approve.mode"), b"on\n")
    before = read(os.path.join(cfg, "settings.json"))
    code, out = install(cfg)
    return (code == 0 and len(ours(settings(cfg))) == 1 and mode(cfg) == "on"
            and read(os.path.join(cfg, "settings.json")) == before and len(backups(cfg)) == 1
            and "没有改动" in out), out


def case_set_mode():
    cfg = fresh("mode")
    install(cfg)
    code, out = install(cfg, "--mode", "on")
    return code == 0 and mode(cfg) == "on" and "已开启" in out, out


def case_mode_quick():
    cfg = fresh("mode-quick")
    install(cfg)
    dst = os.path.join(cfg, "hooks", "auto-approve.py")
    with open(dst, "a") as f:
        f.write("\n# MY LOCAL TWEAK\n")
    hook_before, settings_before, backups_before = read(dst), read(os.path.join(cfg, "settings.json")), backups(cfg)
    code, out = install(cfg, "--mode", "on")
    return (code == 0 and mode(cfg) == "on" and read(dst) == hook_before
            and read(os.path.join(cfg, "settings.json")) == settings_before and backups(cfg) == backups_before
            and "钩子脚本" not in out), out


def case_windows_entry():
    import install as inst
    saved = inst.WINDOWS
    inst.WINDOWS = True
    try:
        path = "C:\\Users\\me\\.claude\\hooks\\auto-approve.py"
        bash_entry = inst.hook_entry("python", path, powershell=False)["hooks"][0]
        ps_entry = inst.hook_entry("python", path, powershell=True)["hooks"][0]
        quoted = inst.hook_entry('"C:/Program Files/Python312/python.exe"', path, powershell=True)["hooks"][0]
    finally:
        inst.WINDOWS = saved
    ok = (bash_entry["command"] == 'python "C:/Users/me/.claude/hooks/auto-approve.py"' and "shell" not in bash_entry
          and ps_entry.get("shell") == "powershell" and ps_entry["command"] == bash_entry["command"]
          and quoted["command"].startswith('& "C:/Program Files/'))
    return ok, "%s\n%s\n%s" % (bash_entry, ps_entry, quoted)


def case_mode_off_message():
    cfg = fresh("mode-off")
    code, out = install(cfg, "--mode", "off")
    return code == 0 and mode(cfg) == "off" and "暂停" in out and "已开启" not in out, out


def case_config_dir_mode_respected():
    cfg = fresh("cfgdir-mode")
    install(cfg, "--mode", "off")
    off_ok = not run_registered(cfg, "ls", override_mode=False)
    install(cfg, "--mode", "on")
    on_ok = run_registered(cfg, "ls", override_mode=False)
    return off_ok and on_ok, "off 时不该放行：%s；on 时该放行：%s" % (off_ok, on_ok)


def case_backup_unique():
    cfg = fresh("backup")
    write(os.path.join(cfg, "settings.json"), {"theme": "ORIGINAL"})
    py = "python" if WINDOWS else "python3"
    install(cfg, "--python", py)
    code, out = install(cfg, "--python", py + " -B")
    names = backups(cfg)
    first = json.loads(read(os.path.join(cfg, names[0])).decode()) if names else {}
    return code == 0 and len(names) == 2 and first == {"theme": "ORIGINAL"}, "%s\n%s" % (names, out)


def case_bom():
    cfg = fresh("bom")
    write(os.path.join(cfg, "settings.json"), b'\xef\xbb\xbf{"theme": "dark"}')
    code, out = install(cfg)
    data = settings(cfg)
    return code == 0 and data["theme"] == "dark" and len(ours(data)) == 1, out


def case_bad_json():
    cfg = fresh("bad")
    write(os.path.join(cfg, "settings.json"), b'{"theme": "dark",}')
    code, out = install(cfg)
    return (code != 0 and read(os.path.join(cfg, "settings.json")) == b'{"theme": "dark",}'
            and not os.path.exists(os.path.join(cfg, "hooks")) and "Traceback" not in out), out


def case_utf16_settings():
    cfg = fresh("utf16")
    raw = '{"theme": "dark"}'.encode("utf-16")
    write(os.path.join(cfg, "settings.json"), raw)
    code, out = install(cfg)
    return (code != 0 and read(os.path.join(cfg, "settings.json")) == raw and "UTF-8" in out
            and "Traceback" not in out), out


def case_odd_entries_kept():
    cfg = fresh("odd")
    odd = [1, "x", None, {"matcher": "Bash", "hooks": None}, {"matcher": "Read", "hooks": []}]
    write(os.path.join(cfg, "settings.json"), {"hooks": {"PermissionRequest": odd}})
    code1, out1 = install(cfg)
    kept = settings(cfg)["hooks"]["PermissionRequest"][:5] == odd
    code2, out2 = install(cfg, "--uninstall")
    return (code1 == 0 and kept and code2 == 0 and settings(cfg)["hooks"]["PermissionRequest"] == odd
            and "Traceback" not in out2), out1 + out2


def case_not_ours_kept():
    cfg = fresh("not-ours")
    theirs = [{"matcher": "Bash", "hooks": [{"type": "command", "command": c}]} for c in (
        "python3 /opt/other-project/auto-approve.py",
        "python3 /opt/tools/my-auto-approve.py",
        "bash -c 'grep -q auto-approve.py /var/log/x || true'",
    )]
    write(os.path.join(cfg, "settings.json"), {"hooks": {"PermissionRequest": theirs}})
    install(cfg)
    after_install = settings(cfg)["hooks"]["PermissionRequest"][:3] == theirs
    code, out = install(cfg, "--uninstall")
    return code == 0 and after_install and settings(cfg)["hooks"]["PermissionRequest"] == theirs, out


def case_legacy_replaced():
    cfg = fresh("legacy")
    legacy = {"matcher": "Bash", "hooks": [{"type": "command", "command": "/home/someone/.claude/hooks/auto-approve.py"}]}
    write(os.path.join(cfg, "settings.json"), {"hooks": {"PermissionRequest": [legacy]}})
    code, out = install(cfg)
    entries = settings(cfg)["hooks"]["PermissionRequest"]
    return code == 0 and len(entries) == 1 and legacy not in entries and "换掉了原来登记的" in out, out


def case_user_edit_backed_up():
    cfg = fresh("edited")
    install(cfg)
    dst = os.path.join(cfg, "hooks", "auto-approve.py")
    with open(dst, "a") as f:
        f.write("\n# MY LOCAL TWEAK\n")
    code, out = install(cfg)
    saved = [f for f in os.listdir(os.path.join(cfg, "hooks")) if f.startswith("auto-approve.py.bak-")]
    restored = read(dst) == read(os.path.join(ROOT, "auto-approve.py"))
    return code == 0 and len(saved) == 1 and restored, out


def case_uninstall():
    cfg = fresh("uninstall")
    write(os.path.join(cfg, "settings.json"), {"hooks": {"PermissionRequest": [OTHER]}})
    install(cfg)
    code, out = install(cfg, "--uninstall")
    data = settings(cfg)
    return (code == 0 and data["hooks"]["PermissionRequest"] == [OTHER] and not ours(data)
            and not os.path.exists(os.path.join(cfg, "hooks", "auto-approve.py"))
            and os.path.exists(os.path.join(cfg, "auto-approve.mode"))), out


def case_uninstall_cleans_empty():
    cfg = fresh("uninstall-empty")
    write(os.path.join(cfg, "settings.json"), {"theme": "dark"})
    install(cfg)
    code, out = install(cfg, "--uninstall")
    return code == 0 and settings(cfg) == {"theme": "dark"}, out


def case_registered_command_runs():
    cfg = fresh("e2e")
    install(cfg)
    ok = run_registered(cfg, "ls -la") and not run_registered(cfg, "rm -rf ~")
    return ok, "登记的命令：%s" % ours(settings(cfg))


def case_self_check_catches_bad_python():
    cfg = fresh("bad-python")
    code, out = install(cfg, "--python", "no-such-python-xyz")
    return code != 0 and "自检没过" in out, out


def case_empty_config_dir():
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, INSTALL, "--config-dir", "", "--skip-tests"],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    return p.returncode != 0, p.stdout.decode("utf-8", errors="replace")


def case_symlink():
    if WINDOWS:
        return True, "Windows 上跳过"
    cfg = fresh("symlink")
    real = os.path.join(TMP, "dotfiles-settings.json")
    write(real, {"theme": "dark"})
    os.symlink(real, os.path.join(cfg, "settings.json"))
    code, out = install(cfg)
    with open(real) as f:
        real_data = json.load(f)
    return code == 0 and os.path.islink(os.path.join(cfg, "settings.json")) and len(ours(real_data)) == 1, out


def case_stale_shell():
    if WINDOWS:
        return True, "Windows 上跳过"
    cfg = fresh("stale-shell")
    code, out = install(cfg, env_extra={"SHELL": "/nonexistent/zsh"})
    return code == 0 and "自检通过（sh）" in out, out


def case_odd_dir_name():
    if WINDOWS:
        return True, "Windows 上跳过"
    cfg = fresh('cfg$HOME_x "q')
    code, out = install(cfg)
    return code == 0 and run_registered(cfg, "ls"), out


CASES = [
    (case_fresh, "全新安装"),
    (case_keep_existing, "保留原有配置和别人的钩子"),
    (case_reinstall, "重复安装：不重复登记、不改开关、不多备份"),
    (case_set_mode, "--mode on 切开关"),
    (case_mode_quick, "已装好时 --mode 只切开关、不重装"),
    (case_windows_entry, "Windows 的登记格式（Git Bash / PowerShell）"),
    (lambda: case_c_drive_guard("windows"), "Windows：登记执行前检查，卸载时摘干净"),
    (lambda: case_c_drive_guard("wsl"), "WSL：登记执行前检查，卸载时摘干净"),
    (case_mode_off_message, "--mode off 提示是暂停"),
    (case_config_dir_mode_respected, "--config-dir 装的，开关也认"),
    (case_backup_unique, "同一秒两次改动，备份不互相覆盖"),
    (case_bom, "settings.json 带 BOM"),
    (case_bad_json, "坏 JSON：停下，什么都不动"),
    (case_utf16_settings, "UTF-16 的 settings.json：说清楚原因"),
    (case_odd_entries_kept, "格式奇怪的条目原样保留"),
    (case_not_ours_kept, "别人同名的钩子不误删"),
    (case_legacy_replaced, "手动登记的会被换掉"),
    (case_user_edit_backed_up, "装着的脚本被改过，先备份"),
    (case_uninstall, "卸载只摘自己"),
    (case_uninstall_cleans_empty, "卸载后不留空的 hooks"),
    (case_registered_command_runs, "登记的命令经 shell 真能跑"),
    (case_self_check_catches_bad_python, "自检能发现 python 找不到"),
    (case_empty_config_dir, "--config-dir 为空时拒绝"),
    (case_symlink, "settings.json 是软链接"),
    (case_stale_shell, "$SHELL 失效时退回 /bin/sh"),
    (case_odd_dir_name, "配置目录名里有 $ 和引号"),
]


def main():
    failures = []
    for fn, label in CASES:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, repr(e)
        print("  %-34s %s" % (label, "✓" if ok else "✗"))
        if not ok:
            failures.append(label)
            print("    " + str(detail).replace("\n", "\n    "))
    print()
    if failures:
        print("失败 %d/%d 项：%s" % (len(failures), len(CASES), "、".join(failures)))
        return 1
    print("全部 %d 项通过" % len(CASES))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
