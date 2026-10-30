#
"""
NOTE：value 中的 # 和 ; 会被下游 INI 解析器当作注释。
本模块不做转义，需要保留的请用引号（如果目标解析器支持）。
"""

import re

SECTION_RE = re.compile(r'^\[([^]]+)]\s*(?:[#;].*)?$')  # r'^\[([^\]]+)\]\s*(?:[#;].*)?$'
KEY_RE = re.compile(r'^(?P<prefix>(?P<key>[^=:#\s][^=:#]*?)\s*[=:]\s*)(?P<value>.*)$')
COMMENT_RE = re.compile(r'^\s*[#;]')
BLANK_RE = re.compile(r'^\s*$')

DELETE = object()


def _section_name(line):
    m = SECTION_RE.match(line)
    return m.group(1).strip() if m else None


def _match_key(line):
    # if line[:1] in (' ', '\t'):
    #     return None
    if COMMENT_RE.match(line) or BLANK_RE.match(line):
        return None
    m = KEY_RE.match(line)
    if not m:
        return None
    return m.group('key').strip(), m.group('prefix')


def transform(lines, edits, eol='\n'):
    """
    惰性生成器：消费原始行，yield 修改后的行。

    edits: {section: {key: value 或 DELETE}}
           value 是字符串 → 替换 / 新增
           value 是 DELETE → 删除该 key（含续行）
    """
    current_section = None
    pending = {}
    seen_sections = set()
    skip_continuation = False
    blank_buffer = []  # 暂存"还没决定去留"的空行

    for line in lines:
        is_blank = line.strip() == ''

        # 1. skip 模式：吞掉被删/被替换 key 的续行
        if skip_continuation:
            if is_blank:
                blank_buffer.append(line)
                continue
            if line[:1] in (' ', '\t'):
                blank_buffer.clear()
                continue
            skip_continuation = False

        # 2. section 头
        sec = _section_name(line)
        if sec is not None:
            # 先补 pending，再吐暂存空行
            for k, v in pending.items():
                if v is not DELETE:
                    yield f'{k} = {v}{eol}'
            pending.clear()
            for b in blank_buffer:
                yield b
            blank_buffer.clear()

            current_section = sec
            seen_sections.add(sec)
            if sec in edits:
                pending.update(edits[sec])
            yield line
            continue

        # 3. 空行先攒着
        if is_blank:
            blank_buffer.append(line)
            continue

        # 4. 非空行：先吐出攒下的空行
        for b in blank_buffer:
            yield b
        blank_buffer.clear()

        # 5. 命中待编辑 key
        if current_section is not None and pending:
            mk = _match_key(line)
            if mk is not None:
                k, prefix = mk
                if k in pending:
                    action = pending.pop(k)
                    if action is DELETE:
                        skip_continuation = True
                        continue
                    yield f'{prefix}{action}{eol}'
                    skip_continuation = True
                    continue

        # 6. 其他原样
        yield line

    # EOF：先补 pending，再吐空行
    for k, v in pending.items():
        if v is not DELETE:
            yield f'{k} = {v}{eol}'
    pending.clear()
    for b in blank_buffer:
        yield b
    blank_buffer.clear()

    # 追加文件中不存在的 section
    for sec, kvs in edits.items():
        if sec in seen_sections:
            continue
        yield f'{eol}[{sec}]{eol}'
        for k, v in kvs.items():
            if v is not DELETE:
                yield f'{k} = {v}{eol}'


if __name__ == '__main__':
    # ══════════════════════════════════════════════════════════════
    # 测试
    # ══════════════════════════════════════════════════════════════

    def _run(lines, edits, eol='\n'):
        return list(transform(lines, edits, eol=eol))


    def _test(name, fn):
        try:
            fn()
            print(f'  PASS  {name}')
            return True
        except AssertionError as e:
            print(f'  FAIL  {name}')
            print(f'        {e}')
            return False
        except Exception as e:
            print(f'  ERROR {name}: {type(e).__name__}: {e}')
            return False


    # ── 基础 ──────────────────────────────────────────────────────

    def t_no_edits_passthrough():
        src = ['[S]\n', 'a = 1\n', 'b = 2\n', '\n', '[T]\n', 'c = 3\n']
        assert _run(src, {}) == src


    def t_empty_input():
        assert _run([], {}) == []


    # ── 替换 value ───────────────────────────────────────────────

    def t_replace_value_keeps_prefix():
        src = ['[S]\n', 'a = 1\n', 'b : 2\n']
        out = _run(src, {'S': {'a': '999'}})
        assert out == ['[S]\n', 'a = 999\n', 'b : 2\n']


    def t_replace_value_then_continuation_dropped():
        src = ['[S]\n', 'a = 1\n', '  cont1\n', '  cont2\n', 'b = 2\n']
        out = _run(src, {'S': {'a': 'X'}})
        assert out == ['[S]\n', 'a = X\n', 'b = 2\n']


    # ── 修复点 2：删除 key 后的续行中间有空行 ─────────────────────

    def t_delete_key_drops_continuation():
        src = ['[S]\n', 'key = v\n', '  cont1\n', '  cont2\n', 'next = x\n']
        out = _run(src, {'S': {'key': DELETE}})
        assert out == ['[S]\n', 'next = x\n']


    def t_delete_key_with_blank_inside_continuation():
        src = [
            '[S]\n',
            'key = v\n',
            '  cont1\n',
            '\n',
            '  cont2\n',
            'next = x\n',
        ]
        out = _run(src, {'S': {'key': DELETE}})
        assert out == ['[S]\n', 'next = x\n']


    def t_delete_key_with_trailing_blank_before_next_key():
        src = [
            '[S]\n',
            'key = v\n',
            '  cont1\n',
            '\n',
            'next = x\n',
        ]
        out = _run(src, {'S': {'key': DELETE}})
        assert out == ['[S]\n', '\n', 'next = x\n']


    def t_delete_key_before_section_header():
        src = [
            '[S]\n',
            'key = v\n',
            '  cont\n',
            '\n',
            '[T]\n',
            'a = 1\n',
        ]
        out = _run(src, {'S': {'key': DELETE}})
        assert out == ['[S]\n', '\n', '[T]\n', 'a = 1\n']


    def t_delete_key_at_eof_with_continuation():
        src = ['[S]\n', 'a = 1\n', 'key = v\n', '  cont\n']
        out = _run(src, {'S': {'key': DELETE}})
        assert out == ['[S]\n', 'a = 1\n']


    def t_delete_only_key_leaves_empty_section():
        src = ['[S]\n', 'key = v\n', '[T]\n']
        out = _run(src, {'S': {'key': DELETE}})
        assert out == ['[S]\n', '[T]\n']


    # ── 修复点 5：新 key 补在空行之前 ────────────────────────────

    def t_new_key_appended_before_blank_line():
        src = ['[old]\n', 'a = 1\n', '\n', '[new]\n', 'b = 2\n']
        out = _run(src, {'old': {'c': '3'}})
        assert out == [
            '[old]\n',
            'a = 1\n',
            'c = 3\n',
            '\n',
            '[new]\n',
            'b = 2\n',
        ]


    def t_new_key_appended_before_multiple_blank_lines():
        src = ['[old]\n', 'a = 1\n', '\n', '\n', '[new]\n']
        out = _run(src, {'old': {'b': '2'}})
        assert out == ['[old]\n', 'a = 1\n', 'b = 2\n', '\n', '\n', '[new]\n']


    def t_new_key_appended_at_eof_before_blank():
        src = ['[S]\n', 'a = 1\n', '\n']
        out = _run(src, {'S': {'b': '2'}})
        assert out == ['[S]\n', 'a = 1\n', 'b = 2\n', '\n']


    def t_new_key_appended_at_eof_no_blank():
        src = ['[S]\n', 'a = 1\n']
        out = _run(src, {'S': {'b': '2'}})
        assert out == ['[S]\n', 'a = 1\n', 'b = 2\n']


    # ── 追加不存在的 section ─────────────────────────────────────

    def t_append_new_section():
        src = ['[A]\n', 'a = 1\n']
        out = _run(src, {'B': {'b': '2', 'c': '3'}})
        assert out == ['[A]\n', 'a = 1\n', '\n[B]\n', 'b = 2\n', 'c = 3\n']


    def t_append_new_section_skips_delete():
        src = ['[A]\n', 'a = 1\n']
        out = _run(src, {'B': {'b': '2', 'c': DELETE}})
        assert out == ['[A]\n', 'a = 1\n', '\n[B]\n', 'b = 2\n']


    def t_existing_section_not_appended_again():
        src = ['[A]\n', 'a = 1\n']
        out = _run(src, {'A': {'a': '9'}})
        assert out == ['[A]\n', 'a = 9\n']


    # ── 边界 ─────────────────────────────────────────────────────

    def t_comment_and_blank_ignored_as_key():
        src = ['[S]\n', '# comment\n', '; another\n', '\n', 'a = 1\n']
        out = _run(src, {'S': {'a': '2'}})
        assert out == ['[S]\n', '# comment\n', '; another\n', '\n', 'a = 2\n']


    def t_indented_line_not_treated_as_key():
        src = ['[S]\n', 'a = 1\n', '  b = 2\n', 'c = 3\n']
        out = _run(src, {'S': {'b': 'X', 'c': 'Y'}})
        # 缩进的 b 被当作续行 → 不匹配；新 b 追加到 EOF
        assert out == ['[S]\n', 'a = 1\n', '  b = 2\n', 'c = Y\n', 'b = X\n']


    def t_replace_then_delete_in_same_section():
        src = ['[S]\n', 'a = 1\n', 'b = 2\n', 'c = 3\n']
        out = _run(src, {'S': {'a': 'X', 'b': DELETE, 'c': 'Z'}})
        assert out == ['[S]\n', 'a = X\n', 'c = Z\n']


    def t_add_and_delete_mixed_new_key():
        src = ['[S]\n', 'a = 1\n']
        out = _run(src, {'S': {'a': DELETE, 'b': '2'}})
        assert out == ['[S]\n', 'b = 2\n']


    def t_custom_eol():
        src = ['[S]\r\n', 'a = 1\r\n']
        out = _run(src, {'S': {'b': '2'}}, eol='\r\n')
        assert out == ['[S]\r\n', 'a = 1\r\n', 'b = 2\r\n']


    def t_is_lazy_generator():
        g = transform(['[S]\n', 'a = 1\n'], {'S': {'a': '2'}})
        assert hasattr(g, '__next__')
        first = next(g)
        assert first == '[S]\n'


    # ── 运行入口 ─────────────────────────────────────────────────
    tests = [
        ('no_edits_passthrough', t_no_edits_passthrough),
        ('empty_input', t_empty_input),
        ('replace_value_keeps_prefix', t_replace_value_keeps_prefix),
        ('replace_value_then_continuation_dropped', t_replace_value_then_continuation_dropped),
        ('delete_key_drops_continuation', t_delete_key_drops_continuation),
        ('delete_key_with_blank_inside_continuation', t_delete_key_with_blank_inside_continuation),
        ('delete_key_with_trailing_blank_before_next_key', t_delete_key_with_trailing_blank_before_next_key),
        ('delete_key_before_section_header', t_delete_key_before_section_header),
        ('delete_key_at_eof_with_continuation', t_delete_key_at_eof_with_continuation),
        ('delete_only_key_leaves_empty_section', t_delete_only_key_leaves_empty_section),
        ('new_key_appended_before_blank_line', t_new_key_appended_before_blank_line),
        ('new_key_appended_before_multiple_blank_lines', t_new_key_appended_before_multiple_blank_lines),
        ('new_key_appended_at_eof_before_blank', t_new_key_appended_at_eof_before_blank),
        ('new_key_appended_at_eof_no_blank', t_new_key_appended_at_eof_no_blank),
        ('append_new_section', t_append_new_section),
        ('append_new_section_skips_delete', t_append_new_section_skips_delete),
        ('existing_section_not_appended_again', t_existing_section_not_appended_again),
        ('comment_and_blank_ignored_as_key', t_comment_and_blank_ignored_as_key),
        ('indented_line_not_treated_as_key', t_indented_line_not_treated_as_key),
        ('replace_then_delete_in_same_section', t_replace_then_delete_in_same_section),
        ('add_and_delete_mixed_new_key', t_add_and_delete_mixed_new_key),
        ('custom_eol', t_custom_eol),
        ('is_lazy_generator', t_is_lazy_generator),
    ]

    print(f'Running {len(tests)} tests...\n')
    passed = sum(_test(name, fn) for name, fn in tests)
    failed = len(tests) - passed
    print(f'\n{passed} passed, {failed} failed')
