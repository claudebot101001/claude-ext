"""Tests for Telegram HTML formatting utilities."""

from extensions.telegram.formatting import chunk_html, escape_html, md_to_tg_html


class TestEscapeHtml:
    def test_escapes_ampersand(self):
        assert escape_html("a & b") == "a &amp; b"

    def test_escapes_less_than(self):
        assert escape_html("a < b") == "a &lt; b"

    def test_escapes_greater_than(self):
        assert escape_html("a > b") == "a &gt; b"

    def test_escapes_all_together(self):
        assert escape_html("<b>&</b>") == "&lt;b&gt;&amp;&lt;/b&gt;"

    def test_empty_string(self):
        assert escape_html("") == ""

    def test_no_special_chars(self):
        assert escape_html("hello world") == "hello world"

    def test_double_ampersand(self):
        assert escape_html("a && b") == "a &amp;&amp; b"


class TestMdToTgHtml:
    def test_fenced_code_block_with_language(self):
        md = "```python\nprint('hello')\n```"
        result = md_to_tg_html(md)
        assert '<pre><code class="language-python">' in result
        assert "print(&#x27;hello&#x27;)" in result or "print('hello')" in result
        assert "</code></pre>" in result

    def test_fenced_code_block_no_language(self):
        md = "```\nsome code\n```"
        result = md_to_tg_html(md)
        assert "<pre><code>" in result
        assert "some code" in result
        assert "</code></pre>" in result

    def test_fenced_code_block_escapes_html(self):
        md = "```\n<div>&test</div>\n```"
        result = md_to_tg_html(md)
        assert "&lt;div&gt;" in result
        assert "&amp;test" in result

    def test_inline_code(self):
        result = md_to_tg_html("Use `foo()` here")
        assert "<code>foo()</code>" in result

    def test_inline_code_escapes_html_in_surrounding_text(self):
        result = md_to_tg_html("a < b and `code` here")
        assert "a &lt; b" in result
        assert "<code>code</code>" in result

    def test_bold(self):
        result = md_to_tg_html("This is **bold** text")
        assert "<b>bold</b>" in result

    def test_header_h1(self):
        result = md_to_tg_html("# Title")
        assert "<b>Title</b>" in result

    def test_header_h2(self):
        result = md_to_tg_html("## Subtitle")
        assert "<b>Subtitle</b>" in result

    def test_header_h3(self):
        result = md_to_tg_html("### Section")
        assert "<b>Section</b>" in result

    def test_no_italic_for_underscores(self):
        """snake_case should NOT be converted to italic."""
        result = md_to_tg_html("use my_variable_name here")
        assert "<i>" not in result
        assert "my_variable_name" in result

    def test_unclosed_code_block_passthrough(self):
        """Unclosed ``` should be escaped and passed through."""
        md = "```python\nprint('hello')\nno closing fence"
        result = md_to_tg_html(md)
        # Should not have <pre> since block is unclosed
        assert "<pre>" not in result
        assert "&lt;" not in result or "```python" in result

    def test_plain_text_escapes_html(self):
        result = md_to_tg_html("a < b > c & d")
        assert result == "a &lt; b &gt; c &amp; d"

    def test_multiline_mixed(self):
        md = "# Header\n\nSome **bold** text\n\n```python\nx = 1\n```\n\nDone."
        result = md_to_tg_html(md)
        assert "<b>Header</b>" in result
        assert "<b>bold</b>" in result
        assert "<pre><code" in result
        assert "Done." in result

    def test_bold_inside_header(self):
        result = md_to_tg_html("## A **bold** header")
        assert "<b>A <b>bold</b> header</b>" in result

    def test_inline_code_inside_header(self):
        result = md_to_tg_html("## Use `func`")
        assert "<b>Use <code>func</code></b>" in result

    def test_multiple_inline_codes(self):
        result = md_to_tg_html("Use `a` and `b`")
        assert "<code>a</code>" in result
        assert "<code>b</code>" in result

    def test_bold_not_converted_inside_inline_code(self):
        result = md_to_tg_html("Use `**not bold**`")
        assert "<code>**not bold**</code>" in result
        # Should NOT have nested <b> inside <code>
        assert "<b>" not in result.split("<code>")[1].split("</code>")[0]


class TestChunkHtml:
    def test_short_text_single_chunk(self):
        text = "Hello world"
        assert chunk_html(text) == ["Hello world"]

    def test_empty_string(self):
        assert chunk_html("") == [""]

    def test_splits_long_text(self):
        text = "Line\n" * 1000
        chunks = chunk_html(text, max_len=100)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 120  # some overhead for tag closing

    def test_respects_max_len(self):
        text = "A" * 5000
        chunks = chunk_html(text, max_len=200)
        assert len(chunks) > 1

    def test_reopens_pre_tag_across_chunks(self):
        """When splitting inside a <pre> block, close and reopen it."""
        # Build text: <pre> followed by enough lines to force a split
        lines = ["<pre>"] + [f"line {i}" for i in range(200)] + ["</pre>"]
        text = "\n".join(lines)
        chunks = chunk_html(text, max_len=200)
        assert len(chunks) > 1
        # First chunk should close the <pre>
        assert "</pre>" in chunks[0]
        # Second chunk should reopen <pre>
        assert chunks[1].startswith("<pre>")

    def test_nested_pre_code_reopened(self):
        """Nested <pre><code> should be properly closed and reopened."""
        inner = "\n".join(f"x = {i}" for i in range(200))
        text = f'<pre><code class="language-python">{inner}</code></pre>'
        chunks = chunk_html(text, max_len=300)
        assert len(chunks) > 1
        # First chunk closes both tags
        assert "</code></pre>" in chunks[0]
        # Second chunk reopens both
        assert '<pre><code class="language-python">' in chunks[1] or "<pre>" in chunks[1]

    def test_single_chunk_no_extra_close_tags(self):
        """Text that fits in one chunk should not get extra close tags."""
        text = "<b>hello</b>"
        chunks = chunk_html(text, max_len=4000)
        assert chunks == ["<b>hello</b>"]

    def test_plain_text_chunking(self):
        """Plain text without tags should split cleanly."""
        text = "\n".join(f"Line {i}" for i in range(100))
        chunks = chunk_html(text, max_len=200)
        assert len(chunks) > 1
        rejoined = ""
        for chunk in chunks:
            rejoined += chunk + "\n"
        # All original lines should be present
        for i in range(100):
            assert f"Line {i}" in rejoined
