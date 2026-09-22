// Dependency-free static file server for the built UI (tram/ui/dist).
// Started and stopped by run.mjs — no manual steps, no python, no extra deps.
import http from 'node:http'
import { createReadStream } from 'node:fs'
import { stat } from 'node:fs/promises'
import { extname, join, normalize } from 'node:path'

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.woff2': 'font/woff2',
  '.woff': 'font/woff',
  '.ttf': 'font/ttf',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.map': 'application/json',
}

export async function startStaticServer({ root, port = 0 }) {
  const server = http.createServer(async (req, res) => {
    try {
      const url = new URL(req.url, 'http://127.0.0.1')
      let path = decodeURIComponent(url.pathname)
      if (path === '/' || path === '') path = '/index.html'
      const filePath = normalize(join(root, path))
      // Keep the server rooted at dist — never serve anything above it.
      if (!filePath.startsWith(normalize(root))) {
        res.writeHead(403)
        res.end('forbidden')
        return
      }
      let st
      try {
        st = await stat(filePath)
      } catch {
        res.writeHead(404)
        res.end('not found')
        return
      }
      if (st.isDirectory()) {
        res.writeHead(404)
        res.end('not found')
        return
      }
      res.writeHead(200, { 'Content-Type': MIME[extname(filePath)] || 'application/octet-stream' })
      createReadStream(filePath).pipe(res)
    } catch (e) {
      res.writeHead(500)
      res.end(String(e))
    }
  })

  await new Promise((resolve) => server.listen(port, '127.0.0.1', resolve))
  const { port: boundPort } = server.address()
  return {
    base: `http://127.0.0.1:${boundPort}`,
    close: () => new Promise((resolve) => server.close(resolve)),
  }
}