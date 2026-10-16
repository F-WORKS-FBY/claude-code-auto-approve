#!/usr/bin/env python3
"""auto-approve.py 的用例。跑法：python3 tests/test_hook.py（Windows 上用 python）

开关文件和日志都放在临时目录里，不碰真实的 ~/.claude/auto-approve.mode 和日志。
用例字符串故意写在文件里而不是命令行：命令行里出现 sudo、rm -rf ~ 这些字，会被钩子自己拦下来弹窗。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")   # 输出被重定向到不支持中文的编码时，别因为打印崩掉

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "auto-approve.py")
TMP = tempfile.mkdtemp(prefix="auto-approve-test-")
MODE_FILE = os.path.join(TMP, "mode")
LOG_FILE = os.path.join(TMP, "log")
TEST_VARS = ("DIR", "D", "X", "TARGET", "ROOT", "BUILD_DIR")   # 用例里当"没赋值的变量"用，别被真实环境变量干扰


def bash(command):
    return ("Bash", {"command": command, "description": "test"})


def ps(command):
    return ("PowerShell", {"command": command, "description": "test"})


APPROVE = [
    # 日常命令
    (bash("ls -la /tmp"), "普通命令"),
    (bash("echo done && cat src/shutdown.ts"), "文件名里带 shutdown"),
    (bash("pdflatex -interaction=nonstopmode -halt-on-error main.tex"), "-halt-on-error 参数"),
    (bash("git push origin main"), "普通 push"),
    (bash("git push -u origin feature/x"), "push 带 -u"),
    (bash("git push --set-upstream origin dd-feature"), "push 分支名里有 d/f"),
    (bash("git push origin HEAD:refs/for/main"), "push 带 refspec"),
    (bash('git commit -m "clean up the reset logic"'), "提交说明里有 clean/reset"),
    (bash("git clean -n"), "git clean -n 只预览"),
    (bash("git clean -nd"), "git clean -nd 只预览"),
    (bash("npm publish --dry-run"), "npm publish --dry-run"),
    (bash("kubectl delete -f k8s/ --dry-run=client -o yaml"), "kubectl delete --dry-run"),
    (bash("docker run --rm -it ubuntu bash"), "docker run --rm"),
    (bash("docker build -t rmq ."), "镜像名以 rm 开头"),
    (bash("npm rm lodash"), "npm rm 包名"),
    # 删除：目标安全
    (bash("rm -rf /home/ubuntu/projects/demo/build"), "删家目录下深层目录"),
    (bash("rm -rf ./build dist"), "删相对路径"),
    (bash("rm -rf /tmp/claude-test"), "删 /tmp 下的子目录"),
    (bash("D=/home/ubuntu/x/build; rm -rf $D"), "变量代入后安全"),
    (bash("rm -f ~/projects/demo/a.log"), "删 ~ 下两层以上"),
    (bash("rm -rf /c/Users/me/proj/build"), "Git Bash 写法的深层目录"),
    (bash("find . -name '*.pyc' -exec rm {} +"), "find -exec rm"),
    (bash("git rm -r --cached ."), "git rm --cached ."),
    (bash("git rm --cached secrets.env"), "git rm --cached 文件"),
    (ps("Remove-Item -Recurse -Force .\\node_modules"), "PowerShell 递归删项目内目录"),
    (ps("Get-ChildItem C:\\Users\\me\\proj"), "PowerShell 普通命令"),
    # 只是在搜文字（今天日志里的误伤）
    (bash('grep -rn "sudo" scripts/'), "grep 搜 sudo"),
    (bash('cd /x; grep -nE "rm -f|stopInfra|--rm|volume" harness.ts'), "grep 搜 rm"),
    (bash('git grep -nE "docker[^\\n]*\\brm\\b" -- tools/'), "git grep 搜 docker rm"),
    (bash('git log --oneline --grep="git push --force"'), "git log --grep"),
    (bash("rg -n 'DROP TABLE' migrations/"), "rg 搜 DROP TABLE"),
    (bash("B=/x/claude.exe; dd if=$B bs=1 skip=100 count=700 2>/dev/null | tr -c '[:print:]' '.'"), "dd 只读"),
    (bash("dd if=/dev/zero of=/dev/null bs=1M count=100"), "dd 写到 /dev/null"),
    # 只看工具类型的
    (("Bash", {"command": "ls", "description": "no sudo needed"}), "说明里提到 sudo"),
    (("Edit", {"file_path": "/home/ubuntu/a.py", "old_string": "x", "new_string": "sudo rm -rf /"}), "编辑内容不查黑名单"),
    (("Edit", {"file_path": "/home/u/app/scripts/reboot-workers.sh", "old_string": "a", "new_string": "b"}), "编辑的文件名带 reboot"),
    (("Write", {"file_path": "/home/ubuntu/notes.md", "content": "git reset --hard"}), "写入内容不查黑名单"),
    (("NotebookEdit", {"notebook_path": "/home/ubuntu/a.ipynb", "new_source": "x"}), "Notebook 编辑"),
    (("Read", {"file_path": "/home/u/app/src/shutdown.ts"}), "读 shutdown.ts"),
    (("Glob", {"pattern": "**/*.py", "path": "/home/ubuntu"}), "Glob"),
    (("Grep", {"pattern": "DROP TABLE", "path": "/home/ubuntu"}), "Grep 搜 DROP TABLE"),
    (("WebFetch", {"url": "https://example.com", "prompt": "summarize"}), "WebFetch"),
    (("WebSearch", {"query": "kubectl delete pod stuck terminating"}), "WebSearch 查 kubectl delete"),
    (("Skill", {"skill": "code-review"}), "Skill"),
    (("Workflow", {"script": "agent('review this; never run git push --force or sudo')"}), "Workflow 脚本里写着危险词"),
    (("mcp__codegraph__codegraph_explore", {"query": "x", "projectPath": "/home/ubuntu"}), "MCP 工具"),
]

LEAVE = [
    # 不是授权弹窗该自动点的
    (("ExitPlanMode", {"plan": "1. 改 a.py"}), "计划确认"),
    (("AskUserQuestion", {"questions": []}), "选择题"),
    (("EnterPlanMode", {}), "模式切换不在白名单"),
    (("Agent", {"prompt": "x"}), "其它工具不在白名单"),
    # git push 改写远程历史
    (bash("git push -f origin main"), "push -f"),
    (bash("git push --force-with-lease origin main"), "push --force-with-lease"),
    (bash("git push origin :old-branch"), "push :分支（删远程）"),
    (bash("git push origin +main"), "push +分支"),
    (bash("git push --delete origin x"), "push --delete"),
    (bash("git push --mirror backup"), "push --mirror"),
    (bash("git push -uf origin main"), "push -uf"),
    (bash("git push -fu origin main"), "push -fu"),
    (bash("git push origin main -f;"), "push -f 后跟分号"),
    (bash("(git push -f)"), "push -f 在括号里"),
    (bash("ssh host 'cd /srv/repo && git push -f'"), "ssh 里的 push -f"),
    (bash('git push origin "+main"'), "push 带引号的 +分支"),
    (bash('git push origin ":old-branch"'), "push 带引号的 :分支"),
    (bash("git push origin main \\\n  --force"), "push 续行后 --force"),
    (ps("git push origin main `\n  --force"), "PowerShell 续行后 --force"),
    (bash("git -C /home/ubuntu/repo push --force origin main"), "git -C 之后 push --force"),
    (bash("git -c core.sshCommand='ssh -i k' push --force origin main"), "git -c 之后 push --force"),
    (bash("git --no-pager push origin -d feature"), "git --no-pager push -d"),
    # 其它 git
    (bash("git reset --hard HEAD~1"), "reset --hard"),
    (bash("git reset -q --hard HEAD~1"), "reset -q --hard"),
    (bash("git reset HEAD~1 --hard"), "reset ... --hard"),
    (bash("git -C /repo reset --hard origin/main"), "git -C reset --hard"),
    (bash("git clean -fdx"), "git clean"),
    (bash("git -C /repo clean -fdx"), "git -C clean"),
    (bash("git stash drop"), "stash drop"),
    (bash("git -C /repo stash drop"), "git -C stash drop"),
    # 系统、数据库、集群、发布
    (bash("sudo apt install x"), "sudo"),
    (bash("sudo grep x /root/secret"), "sudo grep 不算搜索"),
    (bash("grep foo $(sudo cat /etc/shadow)"), "搜索里带 $(...)"),
    (bash("mkfs.ext4 /dev/vdb"), "mkfs"),
    (bash("dd if=/dev/zero of=/dev/vda"), "dd 写盘"),
    (bash("dd bs=4M if=ubuntu.iso of=/dev/sdb status=progress"), "dd 参数顺序不同"),
    (bash("dd of=/dev/sdb if=/dev/zero bs=1M"), "dd of 在前"),
    (bash("dd if=x of=out.img"), "dd 写镜像文件"),
    (bash("shutdown -h now"), "shutdown"),
    (bash("systemctl reboot"), "systemctl reboot"),
    (bash("chmod -R 777 /srv"), "chmod 777"),
    (bash("chmod 0777 /srv/app"), "chmod 0777"),
    (bash("chmod -Rv 777 /srv/app"), "chmod -Rv 777"),
    (bash("chmod --recursive 777 /srv/app"), "chmod --recursive 777"),
    (bash('psql -c "DROP TABLE users"'), "DROP TABLE（大写）"),
    (bash("kubectl delete pod x"), "kubectl delete"),
    (bash("kubectl -n prod delete deployment api"), "kubectl -n 之后 delete"),
    (bash("kubectl --context prod delete pod api-0"), "kubectl --context 之后 delete"),
    (bash("docker rm -f c"), "docker rm"),
    (bash("docker container rm -f web"), "docker container rm"),
    (bash("docker image rm myimg:latest"), "docker image rm"),
    (bash("docker system prune -a"), "docker system prune"),
    (bash("docker container prune -f"), "docker container prune"),
    (bash("docker image prune -a -f"), "docker image prune"),
    (bash("docker volume rm v"), "docker volume rm"),
    (bash("docker compose down -v"), "docker compose down -v"),
    (bash("docker-compose down --volumes"), "docker-compose down --volumes"),
    (bash("npm publish"), "npm publish"),
    (bash("pnpm publish --access public"), "pnpm publish"),
    (bash("yarn publish --new-version 1.2.3"), "yarn publish"),
    # rm：危险目标
    (bash("rm -rf /"), "rm 根目录"),
    (bash("rm -rf ~"), "rm ~"),
    (bash("rm -rf ~/"), "rm ~/"),
    (bash("rm -rf ~/projects"), "rm ~ 下一层"),
    (bash("rm -rf ~/projects/*"), "rm ~ 下一层/*"),
    (bash("rm -rf ."), "rm ."),
    (bash("rm -rf .."), "rm .."),
    (bash("rm -rf ../.."), "rm ../.."),
    (bash("rm -rf build/.."), "rm build/..（就是 .）"),
    (bash("rm -rf *"), "rm *"),
    (bash("rm -rf ./*"), "rm ./*"),
    (bash("rm -rf /volume/coder"), "rm 两层绝对路径"),
    (bash("rm -rf /volume/coder/*"), "rm 两层绝对路径/*"),
    (bash("rm -rf /tmp"), "rm /tmp"),
    (bash("rm -rf /tmp/*"), "rm /tmp/*"),
    (bash("rm -rf /var/tmp"), "rm /var/tmp"),
    (bash("rm -rf /private/tmp/*"), "rm macOS /private/tmp/*"),
    (bash("rm -rf /tmp/../home"), "rm /tmp/../home"),
    (bash("rm -rf /home/ubuntu/projects"), "rm 家目录绝对路径下一层"),
    (bash("rm -rf /home/ubuntu/*"), "rm 家目录绝对路径/*"),
    (bash("rm -rf /home/ubuntu/proj/.."), "rm 绕回家目录"),
    (bash("rm -rf /Users/me/Documents"), "rm macOS 家目录下一层"),
    (bash("rm -rf /Users/me/*"), "rm macOS 家目录/*"),
    (bash("rm -rf $DIR/"), "rm $DIR/（变量为空）"),
    (bash("rm -rf $DIR/*"), "rm $DIR/*"),
    (bash('rm -rf "$DIR"/*'), 'rm "$DIR"/*（引号在变量上）'),
    (bash('rm -rf "${BUILD_DIR}"/'), 'rm "${BUILD_DIR}"/'),
    (bash('rm -rf "$HOME"'), "rm $HOME"),
    (bash('rm -rf "$HOME"/projects'), 'rm "$HOME"/projects'),
    (bash("rm -rf $TMPDIR/*"), "rm $TMPDIR/*"),
    (bash("rm -rf ${HOME:?}/projects"), "rm ${HOME:?}/projects"),
    (bash('rm -rf "${HOME:?}"/projects'), 'rm "${HOME:?}"/projects'),
    (bash("rm -rf ${TMPDIR:-/tmp}/*"), "rm ${TMPDIR:-/tmp}/*"),
    (bash('TARGET="$HOME/.cache"; rm -rf "$TARGET"'), "变量值里还有变量"),
    (bash("ROOT=$HOME; rm -rf $ROOT/*"), "变量等于 $HOME"),
    (bash('rm -rf "$PWD"'), "rm $PWD"),
    (bash("rm -rf $OLDPWD"), "rm $OLDPWD"),
    (bash("rm -rf $D/*; D=/a/b/c/d"), "赋值在删除之后不算"),
    (bash("D=/srv; rm -rf $D; D=/a/b/c/d"), "以删除时的赋值为准"),
    (bash("D=/x/tmp-y; rm -rf $D"), "变量代入后两层"),
    (bash("rm -rf $(cat list.txt)"), "rm $(...)"),
    (bash("rm -rf `pwd`"), "rm 反引号"),
    (bash("cd /srv/app && rm -rf *"), "&& 后面的 rm"),
    (bash("rm -rf \\\n  /"), "续行"),
    (bash("bash -c 'rm -rf ~/projects'"), "bash -c 里的 rm"),
    (bash('sh -c "rm -rf $HOME"'), "sh -c 里的 rm"),
    (bash("ssh prod 'rm -rf /srv/app'"), "ssh 里的 rm"),
    (bash('ssh prod "rm -rf ~"'), "ssh 里的 rm ~"),
    (bash("docker exec web sh -c 'rm -rf /data/*'"), "docker exec 里的 rm"),
    (bash("kubectl exec pod -- rm -rf /data/*"), "kubectl exec 里的 rm"),
    (bash("/bin/rm -rf ~/projects"), "/bin/rm"),
    (bash("\\rm -rf ~"), "\\rm"),
    (bash('eval "rm -rf $DIR/"'), "eval 里的 rm"),
    # Windows
    (bash("rm -rf /c/Users/me"), "Git Bash 写法的家目录"),
    (bash("rm -rf /c/Users/me/Desktop"), "Git Bash 写法的家目录下一层"),
    (bash("rm -rf /c/Users/me/*"), "Git Bash 写法的家目录/*"),
    (bash('rm -rf "C:\\Users\\me"'), "Windows 写法的家目录"),
    (bash('rm -rf "C:\\Users\\me\\Documents"'), "Windows 写法的家目录下一层"),
    (bash('rm -rf "$USERPROFILE"'), "rm $USERPROFILE"),
    (bash('rm -rf "$USERPROFILE"/*'), 'rm "$USERPROFILE"/*'),
    (bash("rm -rf /c"), "Git Bash 写法的盘符根"),
    (ps("Remove-Item -Recurse -Force C:\\Users\\me\\proj"), "PowerShell 删家目录下一层"),
    (ps("Remove-Item -Recurse -Force ~"), "PowerShell 删 ~"),
    (ps("rm -r -fo C:\\"), "PowerShell rm 盘符根"),
    (ps("rm -r -fo C:"), "PowerShell rm 裸盘符"),
    (ps("Remove-Item C:\\Users\\me -r -fo"), "Remove-Item -r 缩写"),
    (ps("Remove-Item -Path $env:USERPROFILE\\Documents -Rec -Force"), "Remove-Item $env:USERPROFILE"),
    (ps("rm -r -fo $env:USERPROFILE"), "rm $env:USERPROFILE"),
    (ps('rm -Recurse -Force "$env:USERPROFILE\\Desktop"'), "rm 引号里的 $env:"),
    (ps("rm -r -fo $env:TEMP\\*"), "rm $env:TEMP\\*"),
    (ps("rm -r -fo ${env:USERPROFILE}"), "rm ${env:USERPROFILE}"),
    (ps("rmdir -Recurse -Force C:\\Users\\me"), "rmdir 别名"),
    (ps("ri -r -fo C:\\"), "ri 别名"),
    (ps("del -r -fo C:\\Users\\me"), "del 别名"),
    (ps("Remove-Item C:\\Users\\me `\n-Recurse -Force"), "PowerShell 续行"),
    (ps("Format-Volume -DriveLetter D"), "PowerShell 格式化"),
    (ps("Stop-Computer -Force"), "PowerShell 关机"),
    # 其它
    (("Bash", {"command": "npm test", "description": "test", "dangerouslyDisableSandbox": True}), "要求在沙箱外运行"),
    (bash("ls | xargs rm -rf"), "xargs rm（目标来自命令输出）"),
    (bash("git ls-files -z '*.orig' | xargs -0 rm -f"), "xargs -0 rm"),
    (("mcp__db__query", {"sql": "drop table x"}), "MCP 参数命中黑名单"),
]

# (权限模式, 工具, 参数, 是否放行, 说明)
MODES = [
    ("plan", "Write", {"file_path": "/home/u/a.py", "content": "x"}, True, "plan 模式：写文件照常放行"),
    ("plan", "Bash", {"command": "ls"}, True, "plan 模式：跑命令照常放行"),
    ("plan", "Bash", {"command": "sudo ls"}, False, "plan 模式：黑名单照常生效"),
    ("plan", "ExitPlanMode", {"plan": "1. 改 a.py"}, False, "plan 模式：方案确认留给你"),
    ("auto", "Bash", {"command": "ls"}, False, "auto 模式：命令交给 auto 模式"),
    ("auto", "Edit", {"file_path": "/home/u/a.py", "old_string": "a", "new_string": "b"}, True, "auto 模式：改文件"),
    ("default", "Bash", {"command": "ls"}, True, "默认模式：跑命令"),
    ("acceptEdits", "Bash", {"command": "ls"}, True, "acceptEdits 模式：跑命令"),
]


def run(tool, tool_input, mode="on", raw=None, env_extra=None, event_extra=None):
    """跑一次钩子，返回 (是否放行, 新增的日志, 退出码)。mode 给 bytes 就原样写进开关文件"""
    if mode is None:
        if os.path.exists(MODE_FILE):
            os.remove(MODE_FILE)
    else:
        with open(MODE_FILE, "wb") as f:
            f.write(mode if isinstance(mode, bytes) else (mode + "\n").encode())
    before = os.path.getsize(LOG_FILE) if os.path.exists(LOG_FILE) else 0
    payload = raw if raw is not None else json.dumps(dict({
        "session_id": "11111111-2222", "cwd": os.path.expanduser("~"),
        "hook_event_name": "PermissionRequest", "tool_name": tool, "tool_input": tool_input,
    }, **(event_extra or {})))
    env = dict(os.environ, AUTO_APPROVE_MODE_FILE=MODE_FILE, AUTO_APPROVE_LOG=LOG_FILE, **(env_extra or {}))
    for name in TEST_VARS:
        env.pop(name, None)
    p = subprocess.run([sys.executable, HOOK], input=payload.encode("utf-8"), env=env, capture_output=True)
    out = p.stdout.decode("utf-8")
    allowed = bool(out.strip()) and json.loads(out)["hookSpecificOutput"]["decision"]["behavior"] == "allow"
    new_log = ""
    if os.path.exists(LOG_FILE):
        with open(LOG_FILE, encoding="utf-8") as f:
            f.seek(before)
            new_log = f.read()
    return allowed, new_log, p.returncode


def main():
    failures = []

    def check(label, ok, detail=""):
        print("  %-26s %s" % (label, "✓" if ok else "✗  " + detail.strip()))
        if not ok:
            failures.append(label)

    print("— 应自动放行 —")
    for (tool, tool_input), label in APPROVE:
        allowed, new_log, _ = run(tool, tool_input)
        check(label, allowed and "已自动确认" in new_log, new_log)
    print("— 应留给你（照常弹窗）—")
    for (tool, tool_input), label in LEAVE:
        allowed, new_log, _ = run(tool, tool_input)
        check(label, not allowed and "跳过" in new_log, new_log)
    print("— 权限模式 —")
    for permission_mode, tool, tool_input, expect, label in MODES:
        allowed, new_log, _ = run(tool, tool_input, event_extra={"permission_mode": permission_mode})
        check(label, allowed == expect, new_log)
    print("— 开关和异常输入 —")
    allowed, new_log, _ = run(*bash("ls"), mode="off")
    check("off：不放行也不记", not allowed and not new_log)
    allowed, new_log, _ = run(*bash("ls"), mode="dryrun")
    check("dryrun：只记日志", not allowed and "[试运行]" in new_log)
    allowed, _, _ = run(*bash("ls"), mode=None)
    check("没有开关文件：按 on", allowed)
    allowed, _, _ = run(*bash("ls"), mode="maybe")
    check("开关写错：按 off", not allowed)
    allowed, _, code = run(None, None, raw="not json")
    check("输入不是 JSON：照常弹窗", not allowed and code == 0)
    allowed, _, _ = run(*bash("ls"), mode="on\r\n".encode("utf-16"))
    check("开关是 UTF-16（PowerShell 5）", allowed)
    allowed, _, _ = run(*bash("ls"), mode=b"\xef\xbb\xbfdryrun")
    check("开关带 UTF-8 BOM", not allowed)
    print("— 中文 Windows（stdin 默认按 GBK 解码）—")
    allowed, _, _ = run(*bash("# 清理旧目录\nrm -rf ~"), env_extra={"PYTHONIOENCODING": "gbk"})
    check("中文注释换行后的 rm ~", not allowed)
    allowed, _, _ = run(*bash("echo 完成"), env_extra={"PYTHONIOENCODING": "gbk"})
    check("带中文的普通命令", allowed)

    print()
    total = len(APPROVE) + len(LEAVE) + len(MODES) + 9
    if failures:
        print("失败 %d/%d 项：%s" % (len(failures), total, "、".join(failures)))
        return 1
    print("全部 %d 项通过" % total)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
    sys.exit(code)
