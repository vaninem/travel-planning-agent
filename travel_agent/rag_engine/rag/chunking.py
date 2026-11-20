"""按 Python 语法结构和 token 预算生成可追溯的代码块。"""

import ast
import re
import symtable
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Iterator
from importlib.util import resolve_name
from itertools import groupby
from pathlib import PurePosixPath
from typing import Any, TypedDict

from ..types import DocumentChunk


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
_DEFINITIONS = (*_FUNCTIONS, ast.ClassDef)
_IMPORTS = (ast.Import, ast.ImportFrom)
_EXPRESSION_SCOPES = (ast.Lambda, ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


class _GlobalWrite(TypedDict):
    name: str
    operation: str
    start: int
    end: int


def _assigned_names(target: ast.expr) -> Iterator[str]:
    if isinstance(target, ast.Name):
        yield target.id
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            yield from _assigned_names(element)
    elif isinstance(target, ast.Starred):
        yield from _assigned_names(target.value)


def _dotted_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        if prefix is not None:
            return f"{prefix}.{node.attr}"
    return None


def _definition_scope(
    parent: symtable.SymbolTable | None,
    node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef,
) -> symtable.SymbolTable | None:
    if parent is None:
        return None
    expected = symtable.Class if isinstance(node, ast.ClassDef) else symtable.Function
    for child in parent.get_children():
        if (child.get_name(), child.get_lineno()) != (node.name, node.lineno):
            continue
        if isinstance(child, expected):
            if isinstance(child, symtable.Function) and ".0" in child.get_parameters():
                continue
            return child
        # 泛型定义的函数或类作用域在类型参数作用域下面。
        for definition in child.get_children():
            if isinstance(definition, expected) and definition.get_name() == node.name:
                return definition
    return None


def _scoped_nodes(
    nodes: Iterable[ast.AST], scope: symtable.SymbolTable | None,
) -> Iterator[tuple[ast.AST, symtable.SymbolTable | None]]:
    """遍历定义体时切换作用域，定义头部仍使用外层作用域。"""
    for node in nodes:
        yield node, scope
        children = ast.iter_child_nodes(node)
        if isinstance(node, _DEFINITIONS):
            body = set(node.body)
            yield from _scoped_nodes((child for child in children if child not in body), scope)
            yield from _scoped_nodes(node.body, _definition_scope(scope, node))
        elif isinstance(node, _EXPRESSION_SCOPES):
            # 保留匿名作用域中的调用名，模块变量归属只使用已匹配的作用域。
            yield from _scoped_nodes(children, None)
        else:
            yield from _scoped_nodes(children, scope)


def _import_aliases(statements: Iterable[ast.stmt], package: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for statement in statements:
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                root = alias.name.partition(".")[0]
                aliases[alias.asname or root] = alias.name if alias.asname else root
        elif isinstance(statement, ast.ImportFrom):
            prefix = "." * statement.level + (f"{statement.module}." if statement.module else "")
            for alias in statement.names:
                if alias.name == "*":
                    continue
                name = prefix + alias.name
                if statement.level and package:
                    try:
                        name = resolve_name(name, package)
                    except ImportError:
                        pass
                aliases[alias.asname or alias.name] = name
    return aliases


def split_python(
    source: str, text: str, max_tokens: int, token_counter: Any, *, parent_max_tokens: int | None = None,
) -> list[DocumentChunk]:
    """按定义和语句切分 Python 原文，记录符号、类归属和模块变量的写入位置。

    相邻导入可以合并。超预算时优先在 AST 语句边界续拆，必要时按 token 预算截取。
    指定父块预算时，超长类按完整类成员或已切好的语句区间缩小父范围。
    模块变量的写入是静态源码记录。语法错误保留文件名和原始错误位置。
    """
    if max_tokens <= 0:
        raise ValueError("代码块的 token 预算必须为正数")
    if parent_max_tokens is not None and (type(parent_max_tokens) is not int or parent_max_tokens < max_tokens):
        raise ValueError("类父块预算必须是大于等于子块预算的整数")
    tree = ast.parse(text, filename=source)
    module_scope = symtable.symtable(text, source, "exec")
    offsets = [0, *(match.end() for match in re.finditer(r"\r\n|[\r\n]", text))]
    path = PurePosixPath(source)
    path_parts = (path.stem,) if path.is_absolute() else (*path.parts[:-1], path.stem)
    module_parts = path_parts[:-1] if path.stem == "__init__" else path_parts
    module_name = ".".join(module_parts)
    package = ".".join(path_parts[:-1])
    aliases = _import_aliases(tree.body, package)
    results: list[DocumentChunk] = []

    def absolute_offset(lineno: int, byte_column: int) -> int:
        line_start = offsets[lineno - 1]
        line_end = offsets[lineno] if lineno < len(offsets) else len(text)
        prefix = text[line_start:line_end].encode("utf-8")[:byte_column].decode("utf-8")
        return line_start + len(prefix)

    def node_span(node: ast.stmt | ast.expr) -> tuple[int, int]:
        start = absolute_offset(node.lineno, node.col_offset)
        if isinstance(node, _DEFINITIONS) and node.decorator_list:
            line_index = node.decorator_list[0].lineno - 1
            while line_index > 0:
                line = text[offsets[line_index]:offsets[line_index + 1]]
                if line.lstrip().startswith("@"):
                    break
                line_index -= 1
            start = offsets[line_index]
        end = absolute_offset(node.end_lineno, node.end_col_offset)
        return start, end

    def is_global(name: str, scope: symtable.SymbolTable | None) -> bool:
        if scope is module_scope:
            return True
        if scope is not None:
            try:
                return scope.lookup(name).is_global()
            except KeyError:
                pass
        return False

    def qualified_name(name: str) -> str:
        return f"{module_name}.{name}" if module_name else name

    writes: list[_GlobalWrite] = []
    calls: list[tuple[int, str]] = []
    global_reads: list[tuple[int, str]] = []
    for node, scope in _scoped_nodes(tree.body, module_scope):
        if isinstance(node, ast.Call):
            name = _dotted_name(node.func)
            if name is not None:
                root, dot, rest = name.partition(".")
                if is_global(root, scope):
                    name = aliases.get(root, root) + dot + rest
                calls.append((absolute_offset(node.lineno, node.col_offset), name))
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and is_global(node.id, scope):
            global_reads.append((absolute_offset(node.lineno, node.col_offset), node.id))

        operation = "assign"
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target]
        elif isinstance(node, ast.AugAssign):
            targets = [node.target]
            operation = "augment"
        elif isinstance(node, ast.NamedExpr):
            targets = [node.target]
        else:
            continue
        start, end = node_span(node)
        names = dict.fromkeys(name for target in targets for name in _assigned_names(target))
        for name in names:
            if is_global(name, scope):
                writes.append({"name": name, "operation": operation, "start": start, "end": end})
                if operation == "augment":
                    global_reads.append((start, name))

    writes.sort(key=lambda write: write["start"])
    write_starts = [write["start"] for write in writes]
    global_names = {write["name"] for write in writes}
    references = sorted(calls + [(start, name) for start, name in global_reads if name in global_names])
    reference_starts = [start for start, _ in references]

    def writes_in(start: int, end: int) -> list[_GlobalWrite]:
        return writes[bisect_left(write_starts, start):bisect_left(write_starts, end)]

    def emit_span(
        start: int, end: int, metadata: dict[str, Any], boundaries: tuple[int, ...] = (),
    ) -> None:
        while start < end:
            prefix = token_counter.truncate(text[start:end], max_tokens)
            if not prefix:
                raise ValueError(f"{source} 的代码块预算放不下下一个字符，位置为 {start}")
            chunk_end = start + len(prefix)
            if chunk_end < end:
                boundary_index = bisect_right(boundaries, chunk_end) - 1
                if boundary_index >= 0 and boundaries[boundary_index] > start:
                    chunk_end = boundaries[boundary_index]
            content = text[start:chunk_end]
            if content.strip():
                chunk_metadata = dict(metadata)
                lower = bisect_left(reference_starts, start)
                upper = bisect_left(reference_starts, chunk_end)
                chunk_metadata["references"] = sorted({name for _, name in references[lower:upper]})
                chunk_writes = writes_in(start, chunk_end)
                if chunk_writes:
                    chunk_metadata["global_writes"] = chunk_writes
                results.append(DocumentChunk(
                    id="", source=source, content=content, start=start, end=chunk_end, metadata=chunk_metadata,
                ))
            start = chunk_end

    def emit_function(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        class_name: str | None = None,
        class_span: tuple[int, int] | None = None,
    ) -> None:
        start, end = node_span(node)
        parent_start, parent_end = class_span or (start, end)
        name = f"{class_name}.{node.name}" if class_name else node.name
        metadata = {
            "symbol": node.name, "qualified_symbol": qualified_name(name),
            "parent_start": parent_start, "parent_end": parent_end,
        }
        if class_name is not None:
            metadata["class_name"] = class_name
        emit_span(start, end, metadata, tuple(node_span(statement)[1] for statement in node.body))

    def limit_class_parent(
        start: int, end: int, member_starts: list[int], children: list[DocumentChunk], budget: int,
    ) -> None:
        """按源码顺序合并相邻的类成员，为类的直接子块设置预算内的父区间。"""
        owned = [
            chunk for chunk in children
            if (chunk.metadata["parent_start"], chunk.metadata["parent_end"]) == (start, end)
        ]

        def member_units() -> Iterator[list[DocumentChunk]]:
            for _, group in groupby(owned, key=lambda chunk: bisect_right(member_starts, chunk.start)):
                member_chunks = list(group)
                member_text = text[member_chunks[0].start:member_chunks[-1].end]
                if token_counter.count(member_text) <= budget:
                    yield member_chunks
                else:
                    for chunk in member_chunks:
                        yield [chunk]

        def set_parent(group: list[DocumentChunk]) -> None:
            for chunk in group:
                chunk.metadata.update(parent_start=group[0].start, parent_end=group[-1].end)

        pending: list[DocumentChunk] = []
        for unit in member_units():
            if pending and token_counter.count(text[pending[0].start:unit[-1].end]) > budget:
                set_parent(pending)
                pending = []
            pending.extend(unit)
        if pending:
            set_parent(pending)

    def emit_class(node: ast.ClassDef, enclosing_class: str | None = None) -> None:
        start, end = node_span(node)
        class_name = f"{enclosing_class}.{node.name}" if enclosing_class else node.name
        metadata = {
            "symbol": node.name, "qualified_symbol": qualified_name(class_name),
            "parent_start": start, "parent_end": end,
        }
        first_child = len(results)
        segment_start = start
        boundaries = []
        member_starts = []
        for member in node.body:
            member_start, member_end = node_span(member)
            member_starts.append(member_start)
            if isinstance(member, _DEFINITIONS):
                emit_span(segment_start, member_start, metadata, tuple(boundaries))
                if isinstance(member, ast.ClassDef):
                    emit_class(member, class_name)
                else:
                    emit_function(member, class_name, (start, end))
                segment_start = member_end
                boundaries.clear()
            else:
                boundaries.append(member_end)
        emit_span(segment_start, end, metadata, tuple(boundaries))
        if parent_max_tokens is not None and token_counter.count(text[start:end]) > parent_max_tokens:
            limit_class_parent(start, end, member_starts, results[first_child:], parent_max_tokens)

    module_bounds = {"parent_start": 0, "parent_end": len(text)}
    imports = []
    for index, node in enumerate(tree.body):
        start, end = node_span(node)
        if isinstance(node, _IMPORTS):
            imports.append((index, start, end))
        elif isinstance(node, ast.ClassDef):
            emit_class(node)
        elif isinstance(node, _FUNCTIONS):
            emit_function(node)
        else:
            metadata = dict(module_bounds)
            node_writes = writes_in(start, end)
            if node_writes:
                name = node_writes[0]["name"]
                metadata.update(symbol=name, qualified_symbol=qualified_name(name))
            emit_span(start, end, metadata)

    group_start = group_end = previous_index = None
    for index, start, end in imports:
        if group_start is not None and (
            index != previous_index + 1 or token_counter.count(text[group_start:end]) > max_tokens
        ):
            emit_span(group_start, group_end, module_bounds)
            group_start = None
        if group_start is None:
            group_start = start
        group_end = end
        previous_index = index
    if group_start is not None:
        emit_span(group_start, group_end, module_bounds)

    return sorted(results, key=lambda chunk: chunk.start)
