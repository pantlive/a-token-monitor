"""从本地调用记录归纳行为和对象；不执行命令，不推断网络发送成功。"""

from __future__ import annotations

import ast
import json
import re
import shlex
from pathlib import Path
from typing import Any, Mapping

_IMAGE_SUFFIXES = {
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
    ".gif",
    ".bmp",
    ".svg",
    ".tif",
    ".tiff",
    ".heic",
}
_CODE_SUFFIXES = {
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".java",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".go",
    ".rs",
    ".rb",
    ".php",
    ".swift",
    ".sh",
    ".sql",
    ".ipynb",
    ".vue",
    ".svelte",
    ".html",
    ".css",
}

# 中文注释：跳过注释和字符串，静态读取包装器中的工具调用及字面量参数。
_JS_STRING = r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`"""
_JS_COMMENT = r"//[^\n]*|/\*[\s\S]*?\*/"
_JS_CALLS = re.compile(
    _JS_COMMENT
    + "|"
    + _JS_STRING
    + r"|(?<![\w$.])tools\s*"
    + r"(?:\.\s*(?P<name>[A-Za-z_$][\w$]*)"
    + r"|\[\s*(?P<quote>[\"'])(?P<key>[A-Za-z_$][\w$]*)(?P=quote)\s*\])"
    + r"\s*(?:\?\.\s*)?\("
)
_JS_TOKENS = re.compile(_JS_COMMENT + "|" + _JS_STRING + r"|[\w$]+|[^\s]")


def _literal(value: str) -> Any:
    """只解析字符串、数组等静态字面量；变量及模板插值留空。"""

    if value.startswith("`"):
        return value[1:-1] if "${" not in value else None
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        try:
            return ast.literal_eval(value)
        except (ValueError, SyntaxError):
            return None


def _static_arguments(source: str) -> dict[str, Any]:
    """读取首个静态参数对象，不解析表达式，也不运行日志里的代码。"""

    tokens = [
        match.group()
        for match in _JS_TOKENS.finditer(source[:20_000])
        if not match.group().startswith(("//", "/*"))
    ]
    if not tokens:
        return {}
    if tokens[0] != "{":
        value = _literal(tokens[0])
        return {"input": value} if isinstance(value, str) else {}
    result: dict[str, Any] = {}
    depth = 1
    index = 1
    while index < len(tokens) and depth:
        token = tokens[index]
        if depth == 1 and index + 2 < len(tokens) and tokens[index + 1] == ":":
            key = _literal(token) if token.startswith(("'", '"')) else token
            # 中文注释：只接受完整的字面量值，排除 "prefix" + variable 等表达式。
            end = index + 3
            value_tokens = [tokens[index + 2]]
            if value_tokens[0] == "[":
                nesting = 1
                while end < len(tokens) and nesting:
                    value_tokens.append(tokens[end])
                    nesting += (tokens[end] == "[") - (tokens[end] == "]")
                    end += 1
            if end < len(tokens) and tokens[end] in {",", "}"}:
                value = _literal("".join(value_tokens))
                if isinstance(key, str) and value is not None:
                    result[key] = value
                index = end
                continue
        depth += (token == "{") - (token == "}")
        index += 1
    return result


def static_tool_calls(code: str) -> list[tuple[str, dict[str, Any]]]:
    """提取包装器中可识别的工具和静态参数；不声称所有分支都已执行。"""

    return [
        (
            match.group("name") or match.group("key"),
            _static_arguments(code[match.end() :]),
        )
        for match in _JS_CALLS.finditer(code)
        if match.group("name") or match.group("key")
    ]


def _activity(
    summary: str, *, target: str = "", basis: str = "parameters"
) -> dict[str, str]:
    """行为描述与文件目标分开存放，调用方可独立隐藏文件名。"""

    return {"summary": summary, "target": target, "basis": basis}


def _file_activity(action: str, paths: list[str]) -> dict[str, str]:
    """依据文件扩展名归纳对象；不能确定类型时保留为文件。"""

    suffixes = {Path(path).suffix.lower() for path in paths}
    if suffixes and suffixes <= _IMAGE_SUFFIXES:
        target_type = "图片"
    elif suffixes and suffixes <= _CODE_SUFFIXES:
        target_type = "代码"
    else:
        target_type = "文件"
    summary = f"发起{target_type}上传" if action == "发起上传" else action + target_type
    return _activity(summary, target="、".join(Path(path).name for path in paths))


def _shell_activity(command: str) -> list[dict[str, str]]:
    """只根据操作符和参数归纳常见行为，不展示或执行原始命令。"""

    # 中文注释：脚本正文不是 shell 子命令，不能把 heredoc 内的示例当作已执行行为。
    first_line = command.splitlines()[0] if command.splitlines() else ""
    heredoc = bool(re.search(r"<<-?\s*['\"]?\w+", first_line))
    try:
        lexer = shlex.shlex(
            first_line if heredoc else command, posix=True, punctuation_chars=";&|"
        )
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return [_activity("用途无法判断", basis="unknown")]
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token and all(char in ";&|" for char in token):
            segments.append([])
        else:
            segments[-1].append(token)
    activities: list[dict[str, str]] = []
    for segment in segments:
        while segment and re.fullmatch(r"[A-Za-z_][\w]*=.*", segment[0], re.S):
            segment.pop(0)
        if not segment:
            continue
        program = Path(segment[0]).name
        args = segment[1:]
        paths = [
            value for value in args if not value.startswith("-") and Path(value).suffix
        ]
        if heredoc:
            activities.append(
                _activity("运行脚本", basis="parameters")
                if program in {"python", "python3", "node", "sh", "bash", "zsh"}
                else _activity("用途无法判断", basis="unknown")
            )
        elif program == "git":
            # 中文注释：git -C/-c 的参数不是子命令，必须跳过，避免误报 push。
            index = 0
            while index < len(args) and args[index].startswith("-"):
                index += (
                    2 if args[index] in {"-C", "-c", "--git-dir", "--work-tree"} else 1
                )
            operation = args[index] if index < len(args) else ""
            if operation == "push":
                summary = "发起代码推送"
            elif operation in {"clone", "fetch", "pull"}:
                summary = "从远端获取代码"
            else:
                summary = "操作本地代码仓库"
            activities.append(_activity(summary))
        elif program == "curl":
            uploads: list[str] = []
            for index, flag in enumerate(args):
                option, _, inline = flag.partition("=")
                value = inline or (args[index + 1] if index + 1 < len(args) else "")
                if option in {"-T", "--upload-file"} and value:
                    uploads.append(value)
                elif option in {"-F", "--form", "-d", "--data", "--data-binary"}:
                    match = re.search(r"(?:^|=)@([^;]+)", value)
                    if match:
                        uploads.append(match.group(1))
            activities.append(
                _file_activity("发起上传", uploads)
                if uploads
                else _activity("发送网络请求")
            )
        elif program == "wget":
            uploads = []
            for index, flag in enumerate(args):
                option, _, inline = flag.partition("=")
                value = inline or (args[index + 1] if index + 1 < len(args) else "")
                if option in {"--post-file", "--body-file"} and value:
                    uploads.append(value)
            activities.append(
                _file_activity("发起上传", uploads)
                if uploads
                else _activity("发送网络请求")
            )
        elif program in {"http", "https"}:
            uploads = []
            for token in args:
                if token.startswith("-") or "://" in token or "@" not in token:
                    continue
                target = token.split("@", 1)[1]
                if target.startswith("/") or Path(target).suffix:
                    uploads.append(target)
            activities.append(
                _file_activity("发起上传", uploads)
                if uploads
                else _activity("发送网络请求")
            )
        elif program == "gh" and args[:2] == ["release", "upload"]:
            positional = [value for value in args[2:] if not value.startswith("-")]
            files = positional[1:]
            activities.append(
                _file_activity("发起上传", files)
                if files
                else _activity("发起文件上传")
            )
        elif program in {"huggingface-cli", "hf"} and "upload" in args[:4]:
            index = args.index("upload")
            positional = [
                value for value in args[index + 1 :] if not value.startswith("-")
            ]
            files = positional[1:]
            activities.append(
                _file_activity("发起上传", files)
                if files
                else _activity("发起文件上传")
            )
        elif program == "docker" and args[:1] == ["push"]:
            activities.append(_activity("发起文件上传"))
        elif program in {"scp", "rsync", "rclone"}:
            positional = [value for value in args if not value.startswith("-")]
            destination = positional[-1] if positional else ""
            remote = bool(re.match(r"[^/]+:", destination))
            activities.append(
                _file_activity(
                    "发起上传",
                    paths[:-1] if paths and paths[-1] == destination else paths,
                )
                if remote
                else _activity("复制或同步文件")
            )
        elif program == "aws" and args[:2] in (["s3", "cp"], ["s3", "sync"]):
            activities.append(
                _file_activity("发起上传", [args[2]])
                if len(args) > 3
                and args[3].startswith("s3://")
                and not args[2].startswith("s3://")
                else _activity("复制或同步文件")
            )
        elif program in {"cat", "head", "tail", "sed", "less"}:
            action = (
                "修改"
                if program == "sed" and any(arg.startswith("-i") for arg in args)
                else "读取"
            )
            activities.append(_file_activity(action, paths))
        elif program in {"rg", "grep", "find"}:
            activities.append(_activity("搜索代码或文件"))
        elif program in {"pytest", "npm", "pnpm", "yarn", "python", "python3"}:
            summary = (
                "运行测试"
                if program == "pytest"
                or any(arg in {"test", "pytest", "unittest"} for arg in args[:3])
                else "运行程序或构建"
            )
            activities.append(_activity(summary))
        else:
            activities.append(_activity("用途无法判断", basis="unknown"))
    return activities or [_activity("用途无法判断", basis="unknown")]


def describe_tool_activity(name: str, arguments: object) -> list[dict[str, str]]:
    """把常见工具参数归纳为行为；无法识别时明确保留未知。"""

    if name in {"exec", "functions.exec", "functions__exec"}:
        calls = static_tool_calls(str(arguments or ""))
        activities = [
            item for tool, args in calls for item in describe_tool_activity(tool, args)
        ]
        # 中文注释：同一包装器内相同的行为线索只展示一次，避免重复描述淹没目的。
        distinct = {
            (item["summary"], item["target"], item["basis"]): item
            for item in activities
        }
        return list(distinct.values()) or [_activity("用途无法判断", basis="unknown")]
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            arguments = {"input": arguments}
    params = arguments if isinstance(arguments, Mapping) else {}
    tool = name.split(".")[-1].split("__")[-1].lower()
    paths = [
        str(params[key])
        for key in ("path", "file_path", "filename", "target_file", "filePath")
        if params.get(key)
    ]
    if tool in {
        "exec_command",
        "bash",
        "shell",
        "run_command",
        "shell_command",
        "run_terminal_command",
    }:
        return _shell_activity(str(params.get("cmd") or params.get("command") or ""))
    if tool in {"view_image", "open_image"}:
        return [
            _activity(
                "查看图片", target=Path(paths[0]).name if paths else "", basis="tool"
            )
        ]
    if tool in {"read", "read_file"}:
        return [_file_activity("读取", paths)]
    if tool in {
        "apply_patch",
        "edit",
        "edit_file",
        "write",
        "write_file",
        "search_replace",
    }:
        if tool == "apply_patch":
            paths.extend(
                re.findall(
                    r"^\*\*\* (?:Update|Add|Delete) File: (.+)$",
                    str(params.get("input") or ""),
                    re.M,
                )
            )
        return [_file_activity("修改", paths)]
    if tool in {"upload_image", "upload_file"}:
        return [
            _file_activity("发起上传", paths)
            if paths
            else _activity(
                "发起上传图片" if tool == "upload_image" else "发起上传文件",
                basis="tool",
            )
        ]
    if tool == "imagegen":
        return [_activity("生成或编辑图片", basis="tool")]
    if tool in {"websearch", "webfetch", "web_fetch"} or (
        tool in {"run", "search", "web_search", "web_search_preview"}
        and ("web" in name or tool != "run")
    ):
        return [_activity("检索或读取网络内容", basis="tool")]
    if tool in {"grep", "read_directory", "glob"}:
        return [_activity("搜索代码或文件", basis="tool")]
    if tool in {
        "wait",
        "write_stdin",
        "shell_output",
        "get_command_or_subagent_output",
    }:
        return [_activity("等待或继续执行中的操作", basis="tool")]
    return [_activity("用途无法判断", basis="unknown")]


def describe_user_text(text: str) -> dict[str, str]:
    """只有明确的代码块才归为代码，用户提到上传不表示执行了上传。"""

    code = re.search(
        r"```(?:python|py|javascript|js|typescript|ts|cpp|c|java|go|rust|sh|sql)\b",
        text,
        re.I,
    )
    return _activity("向模型提供代码" if code else "向模型提供文字", basis="record")
