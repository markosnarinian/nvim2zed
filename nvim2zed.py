#!/usr/bin/env python3
"""nvim2zed - convert Neovim/Vim colorschemes into Zed themes.

The converter drives a headless editor, applies each requested colorscheme and
"dumps" every resolved highlight group (foreground/background/special colors and
attributes such as bold/italic). Neovim is queried through the Lua API
(`nvim_get_hl`), which returns fully-resolved RGB values; Vim is queried through
`synIDattr`. Treesitter capture groups (`@function`, `@string`, ...) are used in
preference to the legacy syntax groups (`Function`, `String`, ...) because Zed's
own syntax token names are modelled on the same Treesitter captures.

The dumped data is then mapped onto the Zed theme schema
(https://zed.dev/schema/themes/v0.2.0.json) and written as a theme-family JSON
file that can be dropped into a Zed extension's `themes/` directory or
`~/.config/zed/themes/` for local use.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ZED_SCHEMA = "https://zed.dev/schema/themes/v0.2.0.json"

# --------------------------------------------------------------------------- #
# Editor drivers: produce a normalised dump structure
#
#   dumps[scheme][requested_background] = {
#       "hl": { "<Group>": {fg,bg,sp,bold,italic,underline,...}, ... },
#       "terminal": { "0": "#rrggbb", ... },   # g:terminal_color_* / ansi list
#       "background": "dark" | "light",        # &background after the scheme load
#   }
# --------------------------------------------------------------------------- #

NVIM_DUMP_LUA = r"""
local req = vim.json.decode(io.open(os.getenv("NVIM2ZED_REQUEST"), "r"):read("*a"))

local function hex(n)
  if n == nil then return nil end
  return string.format("#%06x", n)
end

local function dump_current()
  local hls = {}
  for _, name in ipairs(vim.fn.getcompletion("", "highlight")) do
    local ok, def = pcall(vim.api.nvim_get_hl, 0, { name = name, link = false })
    if ok and def then
      hls[name] = {
        fg = hex(def.fg), bg = hex(def.bg), sp = hex(def.sp),
        bold = def.bold or false,
        italic = def.italic or false,
        underline = (def.underline or def.undercurl or def.underdouble) or false,
        strikethrough = def.strikethrough or false,
        reverse = def.reverse or false,
      }
    end
  end
  local term = {}
  for i = 0, 15 do
    local v = vim.g["terminal_color_" .. i]
    if v ~= nil then term[tostring(i)] = v end
  end
  return { hl = hls, terminal = term, background = vim.o.background }
end

local results = {}
for _, scheme in ipairs(req.schemes) do
  results[scheme] = {}
  for _, bg in ipairs(req.backgrounds) do
    for i = 0, 15 do vim.g["terminal_color_" .. i] = nil end   -- avoid carryover
    pcall(function() vim.o.background = bg end)
    local ok, err = pcall(vim.cmd.colorscheme, scheme)
    if ok then
      results[scheme][bg] = dump_current()
    else
      results[scheme][bg] = { error = tostring(err) }
    end
  end
end

local of = io.open(req.out, "w")
of:write(vim.json.encode(results))
of:close()
"""

# Vim cannot encode JSON the way Neovim can, so it writes a tab-separated table
# (one row per highlight group) plus a couple of metadata rows that Python folds
# back into the normalised structure.
VIM_DUMP_VIM = r"""
function! s:Nvim2zedDump() abort
  set termguicolors
  let l:lines = []
  call add(l:lines, "__bg__\t" . &background)
  if exists('g:terminal_ansi_colors')
    call add(l:lines, "__term__\t" . join(g:terminal_ansi_colors, ','))
  endif
  for l:g in getcompletion('', 'highlight')
    let l:id = synIDtrans(hlID(l:g))
    call add(l:lines, join([
          \ l:g,
          \ synIDattr(l:id, 'fg#'),
          \ synIDattr(l:id, 'bg#'),
          \ synIDattr(l:id, 'sp#'),
          \ synIDattr(l:id, 'bold'),
          \ synIDattr(l:id, 'italic'),
          \ synIDattr(l:id, 'underline'),
          \ synIDattr(l:id, 'reverse'),
          \ ], "\t"))
  endfor
  call writefile(l:lines, $NVIM2ZED_OUT)
endfunction
call s:Nvim2zedDump()
"""


def _editor_binary(editor: str) -> tuple[str, str]:
    """Return (binary_path, kind) where kind is 'nvim' or 'vim'."""
    if editor in ("nvim", "vim"):
        binary = shutil.which(editor)
        if not binary:
            sys.exit(f"error: '{editor}' not found on PATH")
        kind = "nvim" if _is_nvim(binary) else "vim"
        return binary, kind
    # auto
    for cand in ("nvim", "vim"):
        binary = shutil.which(cand)
        if binary:
            return binary, ("nvim" if _is_nvim(binary) else "vim")
    sys.exit("error: neither 'nvim' nor 'vim' found on PATH")


def _is_nvim(binary: str) -> bool:
    try:
        out = subprocess.run(
            [binary, "--version"], capture_output=True, text=True, timeout=15
        ).stdout
    except Exception:
        return False
    return "NVIM" in out.splitlines()[0] if out else False


def _config_args(config: str | None, clean: bool) -> list[str]:
    if clean:
        return ["-u", "NONE", "-i", "NONE"]
    if config:
        return ["-u", config]
    return []  # default: load the user's normal config so plugin schemes resolve


def list_schemes(binary: str, kind: str, cfg: list[str]) -> list[str]:
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "schemes.txt")
        env = {**os.environ, "NVIM2ZED_OUT": out}
        if kind == "nvim":
            cmd = [binary, "--headless", *cfg,
                   "-c", "lua vim.fn.writefile(vim.fn.getcompletion('', 'color'), os.getenv('NVIM2ZED_OUT'))",
                   "-c", "qall!"]
        else:
            cmd = [binary, "-es", *cfg,
                   "-c", 'call writefile(getcompletion("", "color"), $NVIM2ZED_OUT)',
                   "-c", "qa!"]
        subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=120)
        if not os.path.exists(out):
            return []
        names = Path(out).read_text().split()
        return sorted(set(n for n in names if n and not n.startswith(".")))


def dump_nvim(binary: str, cfg: list[str], schemes: list[str],
              backgrounds: list[str], verbose: bool) -> dict:
    with tempfile.TemporaryDirectory() as td:
        lua = os.path.join(td, "dump.lua")
        req = os.path.join(td, "req.json")
        out = os.path.join(td, "out.json")
        Path(lua).write_text(NVIM_DUMP_LUA)
        Path(req).write_text(json.dumps(
            {"schemes": schemes, "backgrounds": backgrounds, "out": out}))
        env = {**os.environ, "NVIM2ZED_REQUEST": req}
        cmd = [binary, "--headless", *cfg,
               "--cmd", "set termguicolors",
               "-c", f"luafile {lua}", "-c", "qall!"]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=600)
        if verbose and proc.stderr.strip():
            print(proc.stderr, file=sys.stderr)
        if not os.path.exists(out):
            sys.exit("error: nvim produced no output\n" + proc.stderr)
        return json.loads(Path(out).read_text())


def dump_vim(binary: str, cfg: list[str], schemes: list[str],
             backgrounds: list[str], verbose: bool) -> dict:
    results: dict = {}
    with tempfile.TemporaryDirectory() as td:
        script = os.path.join(td, "dump.vim")
        Path(script).write_text(VIM_DUMP_VIM)
        for scheme in schemes:
            results[scheme] = {}
            for bg in backgrounds:
                out = os.path.join(td, "out.tsv")
                if os.path.exists(out):
                    os.remove(out)
                env = {**os.environ, "NVIM2ZED_OUT": out}
                cmd = [binary, "-es", *cfg,
                       "-c", "set termguicolors",
                       "-c", f"set background={bg}",
                       "-c", f"silent! colorscheme {scheme}",
                       "-c", f"source {script}", "-c", "qa!"]
                proc = subprocess.run(cmd, env=env, capture_output=True,
                                      text=True, timeout=120)
                if verbose and proc.stderr.strip():
                    print(proc.stderr, file=sys.stderr)
                if not os.path.exists(out):
                    results[scheme][bg] = {"error": "vim produced no output"}
                    continue
                results[scheme][bg] = _parse_vim_tsv(Path(out).read_text())
    return results


def _parse_vim_tsv(text: str) -> dict:
    hl: dict = {}
    term: dict = {}
    background = "dark"
    for line in text.splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        if parts[0] == "__bg__":
            background = parts[1] if len(parts) > 1 else "dark"
            continue
        if parts[0] == "__term__":
            cols = (parts[1] if len(parts) > 1 else "").split(",")
            for i, c in enumerate(cols[:16]):
                if c:
                    term[str(i)] = c
            continue
        name = parts[0]
        parts += [""] * (8 - len(parts))
        _, fg, bg, sp, bold, italic, under, rev = parts[:8]
        hl[name] = {
            "fg": fg or None, "bg": bg or None, "sp": sp or None,
            "bold": bold == "1", "italic": italic == "1",
            "underline": under == "1", "strikethrough": False,
            "reverse": rev == "1",
        }
    return {"hl": hl, "terminal": term, "background": background}


# --------------------------------------------------------------------------- #
# Color helpers
# --------------------------------------------------------------------------- #

def _norm_hex(c: str | None) -> str | None:
    if not c:
        return None
    c = c.strip()
    if not c.startswith("#"):
        c = "#" + c
    if not re.fullmatch(r"#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?", c):
        return None
    return c.lower()


def with_alpha(color: str | None, alpha: str) -> str | None:
    color = _norm_hex(color)
    if not color:
        return None
    return color[:7] + alpha


def luminance(color: str) -> float:
    r = int(color[1:3], 16)
    g = int(color[3:5], 16)
    b = int(color[5:7], 16)
    return 0.299 * r + 0.587 * g + 0.114 * b


def resolve(hl: dict, name: str) -> dict | None:
    """Return a highlight definition with `reverse` applied (fg/bg swapped)."""
    d = hl.get(name)
    if not d:
        return None
    fg, bg = _norm_hex(d.get("fg")), _norm_hex(d.get("bg"))
    sp = _norm_hex(d.get("sp"))
    if d.get("reverse"):
        fg, bg = bg, fg
    return {
        "fg": fg, "bg": bg, "sp": sp,
        "bold": bool(d.get("bold")),
        "italic": bool(d.get("italic")),
        "underline": bool(d.get("underline")),
        "strikethrough": bool(d.get("strikethrough")),
    }


def pick(hl: dict, names: list[str], attr: str) -> str | None:
    for n in names:
        d = resolve(hl, n)
        if d and d.get(attr):
            return d[attr]
    return None


# --------------------------------------------------------------------------- #
# Syntax token map: Zed token -> ordered candidate groups (Treesitter first,
# then the modern markup.* captures, then legacy syntax groups).
# --------------------------------------------------------------------------- #

SYNTAX_MAP: dict[str, list[str]] = {
    "attribute": ["@attribute", "@attribute.builtin", "Identifier"],
    "boolean": ["@boolean", "@constant.builtin.boolean", "Boolean", "Constant"],
    "comment": ["@comment", "Comment"],
    "comment.doc": ["@comment.documentation", "SpecialComment", "Comment"],
    "constant": ["@constant", "@constant.builtin", "Constant"],
    "constructor": ["@constructor", "@constructor.call", "Function"],
    "embedded": ["@none", "Normal"],
    "emphasis": ["@markup.italic", "@text.emphasis", "Italic"],
    "emphasis.strong": ["@markup.strong", "@text.strong", "Bold"],
    "enum": ["@lsp.type.enum", "@type.enum", "@type", "Type"],
    "function": ["@function", "@function.call", "Function"],
    "function.builtin": ["@function.builtin", "@function", "Function"],
    "function.method": ["@function.method", "@method", "@function", "Function"],
    "hint": ["@comment.hint", "DiagnosticHint", "Comment"],
    "keyword": ["@keyword", "@keyword.function", "Keyword", "Statement"],
    "label": ["@label", "Label"],
    "link_text": ["@markup.link.label", "@markup.link", "@text.reference", "Underlined"],
    "link_uri": ["@markup.link.url", "@text.uri", "Underlined"],
    "number": ["@number", "@number.float", "Number", "Float"],
    "operator": ["@operator", "Operator"],
    "predictive": ["@comment", "Comment", "NonText"],
    "preproc": ["@keyword.directive", "@preproc", "PreProc"],
    "primary": ["Normal"],
    "property": ["@property", "@variable.member", "@field", "Identifier"],
    "punctuation": ["@punctuation", "@punctuation.delimiter", "Delimiter"],
    "punctuation.bracket": ["@punctuation.bracket", "Delimiter"],
    "punctuation.delimiter": ["@punctuation.delimiter", "Delimiter"],
    "punctuation.list_marker": ["@markup.list", "@punctuation.special", "Special"],
    "punctuation.special": ["@punctuation.special", "Special"],
    "string": ["@string", "String"],
    "string.escape": ["@string.escape", "@string.special.escape", "SpecialChar"],
    "string.regex": ["@string.regexp", "@string.regex", "String"],
    "string.special": ["@string.special", "Special", "SpecialChar"],
    "string.special.symbol": ["@string.special.symbol", "@symbol", "Identifier"],
    "tag": ["@tag", "@tag.builtin", "Tag"],
    "text.literal": ["@markup.raw", "@text.literal", "String"],
    "title": ["@markup.heading", "@text.title", "Title"],
    "type": ["@type", "@type.builtin", "Type", "StorageClass", "Structure"],
    "type.builtin": ["@type.builtin", "@type", "Type"],
    "variable": ["@variable", "Identifier"],
    "variable.special": ["@variable.builtin", "@variable.member", "Special"],
    "variant": ["@lsp.type.enumMember", "@constant", "Constant"],
}


def build_syntax(hl: dict) -> dict:
    syntax: dict = {}
    for token, candidates in SYNTAX_MAP.items():
        for name in candidates:
            d = resolve(hl, name)
            if d and d["fg"]:
                entry: dict = {"color": d["fg"]}
                if d["italic"]:
                    entry["font_style"] = "italic"
                if d["bold"]:
                    entry["font_weight"] = 700
                syntax[token] = entry
                break
    return syntax


# --------------------------------------------------------------------------- #
# Terminal ANSI colors
# --------------------------------------------------------------------------- #

def build_terminal(hl: dict, term: dict, fg: str, bg: str) -> dict:
    def t(i: int) -> str | None:
        return _norm_hex(term.get(str(i)))

    # Derive a fallback palette from common highlight groups when the scheme
    # does not publish g:terminal_color_* / g:terminal_ansi_colors.
    derived = {
        0: bg,
        1: pick(hl, ["@keyword.exception", "DiagnosticError", "ErrorMsg", "Error"], "fg"),
        2: pick(hl, ["@string", "String", "DiffAdd", "diffAdded"], "fg"),
        3: pick(hl, ["@type", "Type", "WarningMsg", "@constant"], "fg"),
        4: pick(hl, ["@function", "Function", "Directory"], "fg"),
        5: pick(hl, ["@keyword", "Keyword", "Statement", "@constant.builtin"], "fg"),
        6: pick(hl, ["@string.special", "Special", "@operator", "SpecialChar"], "fg"),
        7: fg,
    }
    out: dict = {}
    for i in range(8):
        c = t(i) or derived.get(i)
        if c:
            out[f"terminal.ansi.{_ANSI[i]}"] = c
    for i in range(8, 16):
        c = t(i) or out.get(f"terminal.ansi.{_ANSI[i - 8]}")
        if c:
            out[f"terminal.ansi.bright_{_ANSI[i - 8]}"] = c
    return out


_ANSI = ["black", "red", "green", "yellow", "blue", "magenta", "cyan", "white"]


# --------------------------------------------------------------------------- #
# Build one Zed theme from one dump
# --------------------------------------------------------------------------- #

def build_theme(name: str, dump: dict) -> dict | None:
    if "error" in dump or not dump.get("hl"):
        return None
    hl = dump["hl"]
    term = dump.get("terminal") or {}
    if isinstance(term, list):  # empty Lua table encodes as JSON [] not {}
        term = {str(i): v for i, v in enumerate(term)}

    norm = resolve(hl, "Normal") or {}
    declared = dump.get("background", "dark")
    bg = norm.get("bg") or ("#1e1e1e" if declared == "dark" else "#ffffff")
    fg = norm.get("fg") or ("#d4d4d4" if declared == "dark" else "#202020")
    appearance = "dark" if luminance(bg) < 128 else "light"

    muted = (pick(hl, ["Comment", "NonText", "Conceal", "LineNr"], "fg")
             or with_alpha(fg, "99"))
    accent = pick(hl, ["@function", "Function", "Special", "Identifier", "Title"], "fg") or fg
    border = (pick(hl, ["WinSeparator", "VertSplit", "LineNr"], "fg")
              or with_alpha(fg, "33"))
    float_bg = pick(hl, ["NormalFloat", "Pmenu"], "bg") or bg
    visual_bg = pick(hl, ["Visual"], "bg")
    cursorline_bg = pick(hl, ["CursorLine", "CursorColumn"], "bg")
    pmenu_sel = pick(hl, ["PmenuSel", "PmenuThumb", "Visual"], "bg")

    style: dict = {}

    def put(key: str, value: str | None) -> None:
        if value:
            style[key] = value

    # Base surfaces & text
    put("background", bg)
    style["background.appearance"] = "opaque"
    put("foreground", fg)
    put("surface.background", float_bg)
    put("elevated_surface.background", float_bg)
    put("panel.background", float_bg)
    put("editor.background", bg)
    put("editor.foreground", fg)
    put("editor.gutter.background", pick(hl, ["SignColumn"], "bg") or bg)
    put("editor.subheader.background", float_bg)
    put("text", fg)
    put("text.muted", muted)
    put("text.disabled", pick(hl, ["Conceal", "NonText", "Whitespace"], "fg") or muted)
    put("text.placeholder", muted)
    put("text.accent", accent)

    # Borders
    for k in ("border", "border.variant", "border.disabled", "pane.focused_border",
              "pane_group.border", "panel.focused_border", "scrollbar.thumb.border",
              "scrollbar.track.border"):
        put(k, border)
    put("border.focused", pick(hl, ["MatchParen", "Search"], "bg") or accent)
    put("border.selected", accent)
    put("border.transparent", "#00000000")

    # Editor decorations
    put("editor.line_number", pick(hl, ["LineNr"], "fg") or muted)
    put("editor.active_line_number", pick(hl, ["CursorLineNr"], "fg") or fg)
    put("editor.active_line.background",
        cursorline_bg or with_alpha(fg, "0d"))
    put("editor.highlighted_line.background",
        with_alpha(cursorline_bg or accent, "26"))
    put("editor.invisible", pick(hl, ["NonText", "Whitespace"], "fg") or muted)
    put("editor.indent_guide",
        pick(hl, ["IndentBlanklineChar", "IblIndent", "Whitespace"], "fg") or border)
    put("editor.indent_guide_active",
        pick(hl, ["IndentBlanklineContextChar", "IblScope"], "fg") or muted)
    put("editor.wrap_guide", pick(hl, ["ColorColumn"], "bg") or border)
    put("editor.active_wrap_guide", border)
    put("editor.document_highlight.read_background",
        with_alpha(visual_bg or accent, "44"))
    put("editor.document_highlight.write_background",
        with_alpha(visual_bg or accent, "44"))
    put("editor.document_highlight.bracket_background",
        with_alpha(pick(hl, ["MatchParen"], "bg") or accent, "55"))
    put("search.match_background",
        with_alpha(pick(hl, ["Search", "IncSearch", "CurSearch"], "bg") or accent, "66"))

    # Tabs, bars, panels
    put("tab_bar.background", pick(hl, ["TabLineFill", "StatusLine"], "bg") or float_bg)
    put("tab.inactive_background", pick(hl, ["TabLine", "TabLineFill"], "bg") or float_bg)
    put("tab.active_background", pick(hl, ["TabLineSel"], "bg") or bg)
    put("status_bar.background", pick(hl, ["StatusLine"], "bg") or float_bg)
    put("title_bar.background", pick(hl, ["StatusLine"], "bg") or float_bg)
    put("title_bar.inactive_background", pick(hl, ["StatusLineNC"], "bg") or float_bg)
    put("toolbar.background", bg)

    # Elements (buttons, list rows, inputs)
    put("element.background", float_bg)
    put("element.hover", with_alpha(cursorline_bg or visual_bg or accent, "33"))
    put("element.active", pmenu_sel or with_alpha(accent, "44"))
    put("element.selected", pmenu_sel or with_alpha(accent, "55"))
    put("element.disabled", float_bg)
    put("ghost_element.background", "#00000000")
    put("ghost_element.hover", with_alpha(cursorline_bg or accent, "22"))
    put("ghost_element.active", with_alpha(accent, "33"))
    put("ghost_element.selected", pmenu_sel or with_alpha(accent, "44"))
    put("drop_target.background", with_alpha(visual_bg or accent, "33"))

    # Scrollbar
    put("scrollbar.thumb.background", with_alpha(muted, "44"))
    put("scrollbar.thumb.hover_background", with_alpha(muted, "66"))
    put("scrollbar.track.background", "#00000000")

    # Icons
    put("icon", fg)
    put("icon.muted", muted)
    put("icon.disabled", with_alpha(muted, "99"))
    put("icon.placeholder", muted)
    put("icon.accent", accent)
    put("link_text.hover", accent)

    # Status / diagnostics
    error = pick(hl, ["DiagnosticError", "Error", "ErrorMsg"], "fg")
    warn = pick(hl, ["DiagnosticWarn", "WarningMsg", "Todo"], "fg")
    info = pick(hl, ["DiagnosticInfo", "Directory"], "fg")
    hint = pick(hl, ["DiagnosticHint", "Comment"], "fg")
    added = pick(hl, ["GitSignsAdd", "DiffAdd", "diffAdded", "Added"], "fg") \
        or pick(hl, ["DiffAdd"], "bg")
    changed = pick(hl, ["GitSignsChange", "DiffChange", "Changed"], "fg") \
        or pick(hl, ["DiffChange"], "bg")
    removed = pick(hl, ["GitSignsDelete", "DiffDelete", "diffRemoved", "Removed"], "fg") \
        or pick(hl, ["DiffDelete"], "bg")

    def status(base_key: str, color: str | None, bg_src: str | None = None) -> None:
        if not color:
            return
        style[base_key] = color
        style[f"{base_key}.border"] = color
        if bg_src:
            style[f"{base_key}.background"] = with_alpha(bg_src, "22")

    status("error", error, pick(hl, ["DiffDelete", "DiagnosticError"], "bg"))
    status("warning", warn, pick(hl, ["DiagnosticWarn"], "bg"))
    status("info", info, pick(hl, ["DiagnosticInfo"], "bg"))
    status("hint", hint, pick(hl, ["DiagnosticHint"], "bg"))
    status("success", added)
    status("created", added, pick(hl, ["DiffAdd", "GitSignsAdd"], "bg"))
    status("modified", changed, pick(hl, ["DiffChange", "GitSignsChange"], "bg"))
    status("deleted", removed, pick(hl, ["DiffDelete", "GitSignsDelete"], "bg"))
    status("conflict", warn)
    status("renamed", info)
    status("ignored", muted)
    status("hidden", muted)
    status("unreachable", muted)
    status("predictive", hint or muted)

    # Terminal
    put("terminal.background", bg)
    put("terminal.foreground", fg)
    put("terminal.bright_foreground", fg)
    put("terminal.dim_foreground", muted)
    put("terminal.ansi.background", bg)
    style.update(build_terminal(hl, term, fg, bg))

    # Accents & players
    palette: list[str] = []
    for n in ["@function", "Function", "@keyword", "Keyword", "Statement",
              "@string", "String", "@type", "Type", "@constant", "Constant",
              "Special", "Title", "@property"]:
        c = pick(hl, [n], "fg")
        if c and c != bg and c not in palette:
            palette.append(c)
    if not palette:
        palette = [accent, fg]
    style["accents"] = palette[:8]

    cursor = (pick(hl, ["Cursor", "lCursor", "TermCursor"], "bg")
              or pick(hl, ["Cursor"], "fg") or accent)
    sel = visual_bg and with_alpha(visual_bg, "66")
    players = []
    for i in range(8):
        c = palette[i % len(palette)]
        players.append({
            "cursor": c if i else cursor,
            "background": c if i else cursor,
            "selection": sel or with_alpha(c, "3d"),
        })
    style["players"] = players

    # Syntax
    style["syntax"] = build_syntax(hl)

    return {"name": name, "appearance": appearance, "style": style}


# --------------------------------------------------------------------------- #
# Assemble theme family
# --------------------------------------------------------------------------- #

def prettify(scheme: str) -> str:
    words = re.split(r"[-_\s]+", scheme)
    return " ".join(w[:1].upper() + w[1:] if w else w for w in words)


def _theme_signature(theme: dict) -> tuple:
    s = theme["style"]
    return (s.get("background"), s.get("foreground"),
            tuple(sorted((k, v.get("color")) for k, v in s.get("syntax", {}).items())))


def build_family(dumps: dict, family_name: str, author: str) -> dict:
    themes: list[dict] = []
    errors: list[str] = []
    for scheme, by_bg in dumps.items():
        variants: list[dict] = []
        seen: set = set()
        for _bg, dump in by_bg.items():
            theme = build_theme(prettify(scheme), dump)
            if theme is None:
                continue
            sig = _theme_signature(theme)
            if sig in seen:
                continue
            seen.add(sig)
            variants.append(theme)
        if not variants:
            errors.append(scheme)
            continue
        if len(variants) == 1:
            themes.append(variants[0])
        else:
            for v in variants:
                v["name"] = f"{prettify(scheme)} {v['appearance'].capitalize()}"
                themes.append(v)
    if errors:
        print(f"warning: could not convert: {', '.join(errors)}", file=sys.stderr)
    return {
        "$schema": ZED_SCHEMA,
        "name": family_name,
        "author": author,
        "themes": themes,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> None:
    p = argparse.ArgumentParser(
        prog="nvim2zed",
        description="Convert Neovim/Vim colorschemes into a Zed theme family.")
    p.add_argument("schemes", nargs="*",
                   help="colorscheme name(s) to convert (e.g. habamax tokyonight)")
    p.add_argument("--all", action="store_true",
                   help="convert every colorscheme available to the editor")
    p.add_argument("-o", "--output",
                   help="output file ('-' for stdout). Default: <family>.json")
    p.add_argument("--name", help="theme family name")
    p.add_argument("--author", default="nvim2zed", help="theme family author")
    p.add_argument("--background", choices=["dark", "light", "both"], default="both",
                   help="background variant(s) to dump (default: both)")
    p.add_argument("--editor", choices=["auto", "nvim", "vim"], default="auto")
    p.add_argument("--config", help="editor config to load (passed as -u)")
    p.add_argument("--clean", action="store_true",
                   help="ignore user config (-u NONE); built-in/runtimepath schemes only")
    p.add_argument("--verbose", action="store_true", help="print editor stderr")
    args = p.parse_args()

    binary, kind = _editor_binary(args.editor)
    cfg = _config_args(args.config, args.clean)

    schemes = list(dict.fromkeys(args.schemes))
    if args.all:
        schemes = list_schemes(binary, kind, cfg)
        if not schemes:
            sys.exit("error: no colorschemes found")
    if not schemes:
        p.error("no colorschemes given (pass names or --all)")

    backgrounds = ["dark", "light"] if args.background == "both" else [args.background]

    print(f"converting {len(schemes)} scheme(s) with {kind} ...", file=sys.stderr)
    if kind == "nvim":
        dumps = dump_nvim(binary, cfg, schemes, backgrounds, args.verbose)
    else:
        dumps = dump_vim(binary, cfg, schemes, backgrounds, args.verbose)

    family_name = args.name or (prettify(schemes[0]) if len(schemes) == 1
                                else "nvim2zed themes")
    family = build_family(dumps, family_name, args.author)
    if not family["themes"]:
        sys.exit("error: no themes were produced")

    payload = json.dumps(family, indent=2) + "\n"
    if args.output == "-":
        sys.stdout.write(payload)
    else:
        out = args.output or (re.sub(r"[^\w.-]+", "-", family_name.lower()) + ".json")
        Path(out).write_text(payload)
        print(f"wrote {len(family['themes'])} theme(s) -> {out}", file=sys.stderr)


if __name__ == "__main__":
    main()
