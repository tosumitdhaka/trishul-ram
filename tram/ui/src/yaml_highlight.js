// ── Dependency-free YAML highlighter (cosmetic only) ──────────────────────────
//
// Line-oriented tokenizer for pipeline YAML: keys, quoted and plain scalars,
// numbers/booleans/null, anchors/aliases/tags, env substitutions, inline
// comments, document markers and block scalars. The textarea remains the
// single source of truth; this renders the read-only layer behind it.

const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;' }

function esc(s) {
  return String(s || '').replace(/[&<>]/g, (c) => ESC[c])
}

function wrap(text, cls) {
  return cls ? `<span class="${cls}">${esc(text)}</span>` : esc(text)
}

// Key at the start of a (possibly list-prefixed) line: `name:`, `"q d":`, '- name:'
const KEY_RE = /^(\s*)(-\s+)?("[^"\\]*(?:\\.[^"\\]*)*"|'[^']*'|[^:#\s][^:#]*?)\s*:(?=\s|$)/

// Value tokenizer — order matters.
const VALUE_TOKENS = [
  { re: /^"(?:[^"\\]|\\.)*"?/, cls: 'yv' },          // double-quoted string
  { re: /^'(?:[^'])*'?/, cls: 'yv' },                // single-quoted string
  { re: /^\$\([^)\s]*\)/, cls: 'ys' },               // $(ENV) substitution
  { re: /^\$\{[^}\s]*\}/, cls: 'ys' },                // ${ENV} substitution
  { re: /^&[A-Za-z_][\w-]*|^\*[A-Za-z_][\w-]*|^!!?[\w:.-]+/, cls: 'yn' }, // anchor/alias/tag
  { re: /^(?:true|false|null|~)(?=\s|$)/, cls: 'yb' },
  { re: /^-?\d+(?:\.\d+)?(?=\s|$)/, cls: 'yb' },     // numbers
  { re: /^#[^\n]*$/, cls: 'yc' },                    // comment (must follow space)
  { re: /^[^"'$&*#!]+/, cls: 'yv' },                  // plain run (stops at specials)
]

function tokenizeValue(value) {
  let out = ''
  let rest = value
  let prevSpace = true // a leading '#' after ':' is still a comment
  while (rest.length) {
    if (rest[0] === '#' && !prevSpace) {
      out += wrap(rest, 'yv') // 'a#b' — plain scalar, not a comment
      break
    }
    let matched = false
    for (const t of VALUE_TOKENS) {
      const m = rest.match(t.re)
      if (!m) continue
      out += wrap(m[0], t.cls)
      prevSpace = /\s$/.test(m[0])
      rest = rest.slice(m[0].length)
      matched = true
      break
    }
    if (!matched) { // lone specials (! * & $ # …) — emit as plain
      out += wrap(rest[0], 'yv')
      prevSpace = false
      rest = rest.slice(1)
    }
  }
  return out
}

// Render one line. `block` is the indent of the key that opened the current
// block scalar (null = not inside one). Returns { html, block }.
function renderLine(line, block) {
  const indent = line.length - line.trimStart().length
  if (block !== null && (line.trim() === '' || indent > block)) {
    return { html: wrap(line, 'yv'), block }
  }
  if (line.trim() === '') return { html: '', block: null }

  const keyMatch = line.match(KEY_RE)
  if (keyMatch) {
    const [full, ws, dash, key] = keyMatch
    let html = esc(ws) + esc(dash || '') + wrap(key, 'yk') + ':'
    let rest = line.slice(full.length)
    let nextBlock = null
    const blockScalar = rest.match(/^(\s*)([|>])([+-]?\d*)(.*)$/)
    if (blockScalar && !blockScalar[4].trim()) {
      html += blockScalar[1] + wrap(blockScalar[2] + blockScalar[3], 'yn')
      nextBlock = indent
      rest = ''
    } else {
      html += tokenizeValue(rest)
    }
    return { html, block: nextBlock }
  }

  // No key: list item scalar, document marker or stray value.
  if (line.trim() === '---' || line.trim() === '...') {
    return { html: wrap(line, 'ye'), block: null }
  }
  return { html: tokenizeValue(line), block: null }
}

// Highlight whole documents. Every rendered line is wrapped in a span
// carrying its 1-based line number so error anchoring can mark lines.
export function highlightYaml(text) {
  const lines = String(text || '').split('\n')
  let block = null
  return lines.map((line, i) => {
    const r = renderLine(line, block)
    block = r.block
    return `<span class="yl" data-line="${i + 1}">${r.html}</span>`
  }).join('\n')
}
