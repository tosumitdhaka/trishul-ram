import { esc } from './utils.js'

function myersDiff(a, b) {
  const m = a.length
  const n = b.length
  const max = m + n
  const v = new Array(2 * max + 1).fill(0)
  const trace = []

  for (let d = 0; d <= max; d++) {
    trace.push([...v])
    for (let k = -d; k <= d; k += 2) {
      let x = (k === -d || (k !== d && v[k - 1 + max] < v[k + 1 + max]))
        ? v[k + 1 + max]
        : v[k - 1 + max] + 1
      let y = x - k
      while (x < m && y < n && a[x] === b[y]) {
        x += 1
        y += 1
      }
      v[k + max] = x
      if (x >= m && y >= n) return backtrack(trace, a, b, max)
    }
  }

  return backtrack(trace, a, b, max)
}

function backtrack(trace, a, b, max) {
  const result = []
  let x = a.length
  let y = b.length

  for (let d = trace.length - 1; d >= 0; d--) {
    const v = trace[d]
    const k = x - y
    const prevK = (k === -d || (k !== d && v[k - 1 + max] < v[k + 1 + max])) ? k + 1 : k - 1
    const prevX = v[prevK + max]
    const prevY = prevX - prevK

    while (x > prevX && y > prevY) {
      result.unshift({ type: 'equal', line: a[x - 1] })
      x -= 1
      y -= 1
    }

    if (d > 0) {
      if (x > prevX) {
        result.unshift({ type: 'delete', line: a[x - 1] })
        x -= 1
      } else {
        result.unshift({ type: 'insert', line: b[y - 1] })
        y -= 1
      }
    }
  }

  return result
}

function syncScroll(leftPane, rightPane) {
  if (!leftPane || !rightPane) return

  let syncing = false
  leftPane.onscroll = () => {
    if (syncing) return
    syncing = true
    rightPane.scrollTop = leftPane.scrollTop
    syncing = false
  }
  rightPane.onscroll = () => {
    if (syncing) return
    syncing = true
    leftPane.scrollTop = rightPane.scrollTop
    syncing = false
  }
}

export function renderSideBySideYamlDiff(
  oldYaml,
  newYaml,
  {
    leftPane,
    rightPane,
    statsEl,
    renderLine,
    renderStats,
    emptyLine,
  },
) {
  const hunks = myersDiff(
    String(oldYaml ?? '').split('\n'),
    String(newYaml ?? '').split('\n'),
  )
  let leftHtml = ''
  let rightHtml = ''
  let adds = 0
  let dels = 0
  let leftLine = 1
  let rightLine = 1

  for (const hunk of hunks) {
    if (hunk.type === 'equal') {
      leftHtml += renderLine(leftLine++, hunk.line, 'equal')
      rightHtml += renderLine(rightLine++, hunk.line, 'equal')
    } else if (hunk.type === 'delete') {
      leftHtml += renderLine(leftLine++, hunk.line, 'delete')
      rightHtml += renderLine('', '', 'gap')
      dels += 1
    } else {
      leftHtml += renderLine('', '', 'gap')
      rightHtml += renderLine(rightLine++, hunk.line, 'insert')
      adds += 1
    }
  }

  if (leftPane) leftPane.innerHTML = leftHtml || emptyLine
  if (rightPane) rightPane.innerHTML = rightHtml || emptyLine
  syncScroll(leftPane, rightPane)

  if (statsEl) {
    statsEl.innerHTML = renderStats(adds, dels)
  }
}

export function renderCodeOnlyDiffLine(line, type, classPrefix) {
  const prefix = type === 'delete' ? '- ' : type === 'insert' ? '+ ' : '  '
  const content = type === 'gap' ? '&nbsp;' : `${prefix}${esc(line)}`
  return `<div class="${classPrefix}-line ${type}">${content}</div>`
}

export function renderNumberedDiffLine(lineNo, line, type, classPrefix) {
  const prefix = type === 'delete' ? '- ' : type === 'insert' ? '+ ' : '  '
  const content = type === 'gap' ? '&nbsp;' : `${prefix}${esc(line)}`
  return `
    <div class="${classPrefix}-line ${type}">
      <div class="${classPrefix}-line-num">${lineNo === '' ? '' : lineNo}</div>
      <div class="${classPrefix}-line-code">${content}</div>
    </div>`
}

export function renderDiffStats(adds, dels, classes) {
  return (adds === 0 && dels === 0)
    ? `<span class="${classes.muted}">No changes</span>`
    : `<span class="${classes.insert}">+${adds}</span> <span class="${classes.delete}">-${dels}</span>`
}
