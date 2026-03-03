"""Telegram HTML formatting utilities.

Converts a markdown subset to Telegram-compatible HTML and provides
tag-safe chunking for the 4096-char message limit.
"""

import re

# Telegram supports: <b>, <i>, <u>, <s>, <code>, <pre>, <a>, <tg-spoiler>
# We only use: <b>, <i>, <code>, <pre>


def escape_html(text: str) -> str:
    """Escape HTML special characters. Must run BEFORE any tag insertion."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def md_to_tg_html(text: str) -> str:
    """Convert a markdown subset to Telegram HTML.

    Supported conversions:
    - Fenced code blocks (```lang\\n...\\n```) → <pre><code class="language-lang">...</code></pre>
    - Inline code (`...`) → <code>...</code>
    - Bold (**...**) → <b>...</b>
    - Headers (# ..., ## ...) → <b>...</b> + newline

    Does NOT convert _..._ to italic (too many false positives with snake_case).
    """
    lines = text.split("\n")
    result: list[str] = []
    in_code_block = False
    code_block_lang = ""
    code_block_lines: list[str] = []

    for line in lines:
        if not in_code_block:
            # Check for fenced code block start
            m = re.match(r"^```(\w*)\s*$", line)
            if m:
                in_code_block = True
                code_block_lang = m.group(1)
                code_block_lines = []
                continue

            # Process non-code-block line
            result.append(_convert_inline(line))
        else:
            # Check for fenced code block end
            if line.rstrip() == "```":
                in_code_block = False
                escaped_code = escape_html("\n".join(code_block_lines))
                if code_block_lang:
                    result.append(
                        f'<pre><code class="language-{escape_html(code_block_lang)}">'
                        f"{escaped_code}</code></pre>"
                    )
                else:
                    result.append(f"<pre><code>{escaped_code}</code></pre>")
                continue
            code_block_lines.append(line)

    # Unclosed code block — dump as-is (escaped)
    if in_code_block:
        result.append(escape_html("```" + code_block_lang))
        for cl in code_block_lines:
            result.append(escape_html(cl))

    return "\n".join(result)


def _convert_inline(line: str) -> str:
    """Convert a single non-code-block line: headers, bold, inline code."""
    # Headers: # ... → <b>...</b>
    m = re.match(r"^(#{1,6})\s+(.+)$", line)
    if m:
        header_text = escape_html(m.group(2))
        # Apply bold/inline-code within header text
        header_text = _apply_inline_formatting(header_text, already_escaped=True)
        return f"<b>{header_text}</b>"

    # Regular line: escape first, then apply inline formatting
    escaped = escape_html(line)
    return _apply_inline_formatting(escaped, already_escaped=True)


def _apply_inline_formatting(text: str, already_escaped: bool = False) -> str:
    """Apply inline code and bold to already-escaped text."""
    if not already_escaped:
        text = escape_html(text)

    # Inline code: `...` → <code>...</code>
    # Process inline code first to protect its contents from bold conversion
    parts: list[str] = []
    pos = 0
    for m in re.finditer(r"`([^`]+)`", text):
        parts.append(_bold_convert(text[pos : m.start()]))
        parts.append(f"<code>{m.group(1)}</code>")
        pos = m.end()
    parts.append(_bold_convert(text[pos:]))
    return "".join(parts)


def _bold_convert(text: str) -> str:
    """Convert **...** to <b>...</b> in already-escaped text."""
    return re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)


# ---------------------------------------------------------------------------
# Tag-safe chunking
# ---------------------------------------------------------------------------

# Tags we track for reopening across chunks
_TAG_RE = re.compile(r"<(/?)(\w+)(?:\s[^>]*)?>")


def chunk_html(text: str, max_len: int = 4000) -> list[str]:
    """Split HTML text into chunks that fit Telegram's message limit.

    Uses a stack-based approach to track open tags, closing them at chunk
    boundaries and reopening in the next chunk.
    """
    if len(text) <= max_len:
        return [text]

    chunks: list[str] = []
    # Split into lines for cleaner breaks
    lines = text.split("\n")
    current_chunk: list[str] = []
    current_len = 0
    tag_stack: list[str] = []  # stack of open tag strings (e.g. '<pre><code ...>')

    for line in lines:
        # +1 for the newline we'll join with
        line_cost = len(line) + (1 if current_chunk else 0)

        # Would this line push us over? Flush current chunk.
        # Reserve space for closing tags
        close_tags = _build_close_tags(tag_stack)
        overhead = len(close_tags)

        if current_chunk and (current_len + line_cost + overhead > max_len):
            # Close open tags and flush
            chunk_text = "\n".join(current_chunk) + close_tags
            chunks.append(chunk_text)
            # Reopen tags for next chunk
            reopen = _build_reopen_tags(tag_stack)
            current_chunk = [reopen] if reopen else []
            current_len = len(reopen)

        # If a single line (+ overhead) exceeds max_len, hard-split it
        if line_cost + overhead > max_len and not current_chunk:
            for sub in _hard_split_line(line, max_len, tag_stack):
                chunks.append(sub)
            continue

        current_chunk.append(line)
        current_len += line_cost

        # Update tag stack based on this line
        _update_tag_stack(tag_stack, line)

    # Flush remainder
    if current_chunk:
        text_part = "\n".join(current_chunk)
        # Don't add close tags at the very end — they should already be closed
        # in the original text. Only add if stack is non-empty (unclosed tags).
        if tag_stack:
            text_part += _build_close_tags(tag_stack)
        chunks.append(text_part)

    return chunks if chunks else [text]


def _update_tag_stack(stack: list[str], text: str) -> None:
    """Update the tag stack based on tags found in text."""
    for m in _TAG_RE.finditer(text):
        is_close = m.group(1) == "/"
        tag_name = m.group(2).lower()

        if is_close:
            # Pop matching tag from stack
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag_name:
                    stack.pop(i)
                    break
        else:
            # Push open tag (store name + full tag for reopening)
            stack.append((tag_name, m.group(0)))


def _build_close_tags(stack: list[str]) -> str:
    """Build closing tags string from stack (reverse order)."""
    if not stack:
        return ""
    return "".join(f"</{name}>" for name, _ in reversed(stack))


def _build_reopen_tags(stack: list[str]) -> str:
    """Build reopening tags string from stack (original order)."""
    if not stack:
        return ""
    return "".join(full_tag for _, full_tag in stack)


def _hard_split_line(line: str, max_len: int, tag_stack: list[str]) -> list[str]:
    """Split a single oversized line into multiple chunks."""
    chunks: list[str] = []
    close_tags = _build_close_tags(tag_stack)
    reopen_tags = _build_reopen_tags(tag_stack)
    overhead = len(close_tags) + len(reopen_tags)
    usable = max_len - overhead

    if usable < 100:
        usable = 100  # minimum usable space

    pos = 0
    while pos < len(line):
        end = pos + usable
        segment = line[pos:end]
        if pos == 0:
            chunk = segment + close_tags
        else:
            chunk = reopen_tags + segment + close_tags
        chunks.append(chunk)
        pos = end

    # Update tag stack for the last segment
    _update_tag_stack(tag_stack, line)

    return chunks
