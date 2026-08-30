# -*- coding: utf-8 -*-
"""收口功能测试：任务全部完成后关闭本工具打开的所有浏览器（2026-08-30）。"""
from pathlib import Path

from src.browser.driver import parse_wmic_list_output


def test_close_parse_matches_only_project_profiles():
    """只匹配本项目 profile 的浏览器进程；用户自己的浏览器不含该路径，零误伤。"""
    root = Path("E:\\proj\\data\\browser_profiles")
    ud = f"--user-data-dir={root}\\acct05"

    main_proc = (
        'CommandLine="C:\\Chrome\\chrome.exe" --remote-debugging-port=19001 '
        f"{ud} --start-maximized about:blank\n"
        "ProcessId=111\n"
    )
    child_proc = (
        f'CommandLine="...chrome.exe" --type=renderer {ud}\n'
        "ProcessId=222\n"
    )
    user_proc = (
        'CommandLine="...chrome.exe" '
        "--user-data-dir=C:\\Users\\me\\Chrome\\User Data\n"
        "ProcessId=333\n"
    )
    text = "\n\n".join([main_proc, child_proc, user_proc])

    # main_only：只取带调试端口的主进程（子进程交给 /T 树杀连带）
    assert parse_wmic_list_output(text, root, main_only=True) == [111]
    # 全量：含子进程，但仍不含用户的 333
    assert set(parse_wmic_list_output(text, root)) == {111, 222}


def test_close_parse_slash_normalization():
    """正斜杠路径（Drive 偶发返回）也能匹配。"""
    root = Path("E:\\proj\\profiles")
    block = ("CommandLine=chrome --user-data-dir=E:/proj/profiles/acct05\n"
             "ProcessId=77\n")
    assert 77 in parse_wmic_list_output(block, root)


def test_close_default_on():
    """收口开关默认开（用户指令 2026-08-30：任务完成后关工具浏览器）。"""
    from src.config import BrowserConfig
    assert BrowserConfig().运行结束关闭浏览器 is True
