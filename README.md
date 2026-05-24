# nvim2zed

Convert Neovim/Vim colorschemes into [Zed](https://zed.dev) themes.

`nvim2zed` drives a headless editor, applies each colorscheme you ask for, and
**dumps every resolved highlight group** — foreground/background/special colors
plus attributes like bold and italic. It then maps those groups onto the
[Zed theme schema](https://zed.dev/schema/themes/v0.2.0.json) and writes a
ready-to-use theme-family JSON file.

Because it reads the colors straight out of a running editor, it works for the
**vast majority of Neovim and Vim colorschemes** — including ones defined purely
in Lua/VimL with computed palettes — rather than trying to parse colorscheme
source files.

> ## ⚠️ AI disclaimer
>
> This project was written almost entirely by an AI assistant (Claude). The code
> has been smoke-tested against Neovim's built-in colorschemes, but it has not
> been exhaustively reviewed line by line by a human. Treat the generated themes
> as a **starting point**: review them, tweak colors to taste, and open an issue
> or PR if you hit a scheme it handles poorly. Use at your own risk.

## Requirements

- Python 3.9+ (standard library only — no `pip install` needed)
- `nvim` (recommended) or `vim` on your `PATH`, built with `+termguicolors`

## Usage

```bash
# Convert a single scheme (produces both light and dark variants if supported)
python3 nvim2zed.py habamax

# Convert several into one theme family
python3 nvim2zed.py tokyonight gruvbox catppuccin -o my-themes.json

# Convert every colorscheme the editor can see
python3 nvim2zed.py --all -o all-themes.json

# Only the dark variant, written to stdout
python3 nvim2zed.py habamax --background dark -o -
```

### Options

| Flag | Description |
| --- | --- |
| `schemes...` | One or more colorscheme names to convert. |
| `--all` | Convert every colorscheme available to the editor. |
| `-o, --output FILE` | Output path (`-` for stdout). Defaults to `<family>.json`. |
| `--name NAME` | Theme family name. |
| `--author NAME` | Theme family author (default `nvim2zed`). |
| `--background {dark,light,both}` | Background variant(s) to dump (default `both`). |
| `--editor {auto,nvim,vim}` | Which editor to drive (default auto-detect). |
| `--config PATH` | Editor config to load (passed as `-u`). |
| `--clean` | Ignore your user config (`-u NONE`); built-in schemes only. |
| `--verbose` | Print the editor's stderr (useful for debugging a failing scheme). |

### Making installed (plugin) colorschemes visible

By default `nvim2zed` loads your **normal editor config** so any colorscheme you
have installed (via lazy.nvim, packer, vim-plug, …) is available by name. If a
scheme isn't found, make sure it loads in a headless session:

```bash
nvim --headless -c 'colorscheme tokyonight' -c 'qall!'
```

Use `--clean` to restrict to built-in / `runtimepath` schemes, or `--config` to
point at a specific minimal config that installs the scheme.

## Using the output in Zed

The output is a theme **family** file (`$schema`, `name`, `author`, `themes[]`).

- **Local theme:** drop the JSON into `~/.config/zed/themes/` and pick it from
  the theme selector — no extension required.
- **Theme extension:** put the JSON in your extension's `themes/` directory as
  described in the [Zed theme docs](https://zed.dev/docs/extensions/themes).

You can also paste it into the [Zed Theme Builder](https://zed.dev/theme-builder)
to fine-tune colors visually.

### Attribution

If you distribute or publish a theme generated partially or completely with `nvim2zed`, mention that it was created using nvim2zed and include a link to this repository: https://github.com/markosnarinian/nvim2zed

## How it works

1. **Dump.** A headless `nvim`/`vim` is launched, `termguicolors` is enabled, the
   colorscheme is applied, and every highlight group is read back with resolved
   colors:
   - Neovim uses the Lua API `nvim_get_hl(0, { name, link = false })`, which
     returns fully-resolved RGB values and attribute flags.
   - Vim uses `synIDattr(synIDtrans(hlID(group)), 'fg#')` and friends.
2. **Map.** Vim highlight groups are mapped onto Zed's style keys (editor, gutter,
   tabs, panels, diagnostics, git/diff status, terminal ANSI, players, …) and
   syntax tokens. **Treesitter capture groups** (`@function`, `@string`,
   `@punctuation.delimiter`, …) are preferred over the legacy groups
   (`Function`, `String`, …) because Zed's own syntax token names are modelled on
   the same Treesitter captures — falling back to the legacy groups when a
   capture isn't themed.
3. **Derive the UI chrome.** Most Vim colorschemes only theme the buffer, so
   Zed's surrounding UI (panels, borders, buttons, tabs, status bar, scrollbar,
   placeholders) is **derived by tinting the editor background toward the
   foreground** — lightening surfaces on dark themes and darkening them on light
   themes, in layered steps. Scheme-provided colors (`NormalFloat`, `Pmenu`,
   `StatusLine`, `WinSeparator`, …) are used when present, but only when they
   elevate in the right direction and aren't wildly out of range — Vim
   statuslines are often inverted, which looks wrong as a large Zed surface.
4. **Light/dark.** Each scheme is dumped against both backgrounds; appearance is
   decided from the `Normal` background luminance. Schemes that don't react to
   `&background` collapse to a single theme automatically.
5. **Terminal colors.** `g:terminal_color_0..15` (or Vim's
   `g:terminal_ansi_colors`) are used when the scheme sets them, otherwise a
   reasonable ANSI palette is derived from common highlight groups.

## Limitations

- A scheme must define **GUI (truecolor)** colors. Cterm-only schemes that don't
  set `gui*`/`#rrggbb` values will produce sparse themes.
- Plugin colorschemes must be loadable in a headless session (see above).
- Zed's UI is far more granular than a typical Vim colorscheme, so some UI keys
  are synthesised (alpha overlays, derived borders/accents). Expect to fine-tune.
- The Vim (non-Neovim) path is implemented but less thoroughly tested than the
  Neovim path.
