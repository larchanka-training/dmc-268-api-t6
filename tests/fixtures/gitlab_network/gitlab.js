// Local consumed-contract fixture; never deploy: fs/env contain private per-run credentials.
'use strict';
const http = require('http');
const fs = require('fs');
const manifest = JSON.parse(fs.readFileSync(__dirname + '/manifest.json', 'utf8'));
const config = JSON.parse(fs.readFileSync(process.env.T15_PRIVATE_CONFIG, 'utf8'));
const states = new Map();
for (const label of Object.keys(config.tenants)) {
  states.set(label, { generation: 1, mode: 'complete', head2: false, faults: {}, discussions: {}, attempts: {}, events: [] });
}
const refs = manifest.refs;
const send = (res, status, value, headers = {}) => {
  const body = Buffer.from(JSON.stringify(value));
  res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': body.length, ...headers });
  res.end(body);
};
function entry(path, blob) {
  return { id: manifest.blobs[blob].oid, name: path.split('/').pop(), path, type: 'blob', mode: '100644' };
}
function tree(revision, state, recursive, directory) {
  if (directory === '.review') return [entry('.review/rules.md', 'rules')];
  if (!recursive) return [entry('AGENTS.md', 'agents'), ...Object.entries(manifest.directories).map(([path, id]) => ({ id, name: path, path, type: 'tree', mode: '040000' }))];
  const py = revision === refs.merge_base ? entry(manifest.paths.old, 'old') : entry(manifest.paths.new, revision === refs.start ? 'old' : state.head2 ? 'new2' : 'new');
  return [py, ...['javascript', 'unknown', 'empty', 'agents', 'rules'].map(name => entry(manifest.paths[name], name)),
    { id: '4'.repeat(40), name: 'link.py', path: 'link.py', type: 'blob', mode: '120000' },
    { id: '5'.repeat(40), name: 'vendor', path: 'vendor', type: 'commit', mode: '160000' }];
}
function knownBlob(revision, path, state) {
  if (path === manifest.paths.old && revision === refs.merge_base) return manifest.blobs.old;
  if (path === manifest.paths.new && revision !== refs.merge_base) return manifest.blobs[revision === refs.start ? 'old' : state.head2 ? 'new2' : 'new'];
  return manifest.blobs[['javascript', 'unknown', 'empty', 'agents', 'rules'].find(key => manifest.paths[key] === path)];
}
async function read(req) {
  let bytes = 0; const chunks = [];
  for await (const chunk of req) { bytes += chunk.length; if (bytes > 1048576) throw new Error('body cap'); chunks.push(chunk); }
  return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
}
const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, 'http://fixture.invalid');
    if (url.pathname === '/__control') {
      if (req.headers['x-proof-control'] !== config.control_key) return send(res, 403, { error: 'control rejected' });
      const body = await read(req);
      if (req.method !== 'POST' || !states.has(body.tenant)) return send(res, 400, { error: 'control shape' });
      const state = states.get(body.tenant);
      if (body.patch) {
        if (Object.keys(body.patch).some(key => !['generation', 'mode', 'head2', 'faults'].includes(key))) return send(res, 400, { error: 'unknown control' });
        Object.assign(state, body.patch);
      }
      return send(res, 200, { generation: state.generation, mode: state.mode, head2: state.head2, discussions: state.discussions, attempts: state.attempts, events: state.events });
    }
    const token = req.headers['private-token'];
    const label = Object.keys(config.tenants).find(name => config.tenants[name].tokens.includes(token));
    if (!label) return send(res, 403, { error: 'credential scope' });
    const tenant = config.tenants[label], state = states.get(label);
    const origin = req.headers['x-fixture-origin'];
    if (origin !== tenant.origin || req.headers['x-fixture-host'] !== new URL(tenant.origin).host) return send(res, 403, { error: 'origin scope' });
    const target = tenant.project, source = tenant.source_project;
    const management = req.method === 'GET' && ['/api/v4/user', `/api/v4/projects/${target}`, `/api/v4/projects/${target}/languages`].includes(url.pathname);
    if (token !== tenant.tokens[state.generation - 1] && !(management && state.generation === 1 && token === tenant.tokens[1])) return send(res, 403, { error: 'generation scope' });
    const generation = tenant.tokens.indexOf(token) + 1;
    state.events.push({ method: req.method, path: url.pathname, query: url.search, generation });
    if (management) {
      if (url.search) return send(res, 501, { error: 'management query' });
      if (url.pathname.endsWith('/user')) return send(res, 200, { id: 7, state: 'active' });
      if (url.pathname.endsWith('/languages')) return send(res, 200, { Python: 75, JavaScript: 25 });
      return send(res, 200, { id: target, path_with_namespace: 'team/project', default_branch: 'main', web_url: tenant.origin + '/team/project', archived: false, empty_repo: false });
    }
    const mr = new RegExp(`^/api/v4/projects/${target}/merge_requests/([1-9][0-9]{0,3})(?:/(.*))?$`).exec(url.pathname);
    const head = state.head2 ? refs.next_head : refs.head;
    const triple = { head_commit_sha: head, start_commit_sha: refs.start, base_commit_sha: refs.merge_base };
    if (mr) {
      const iid = Number(mr[1]), suffix = mr[2] || '';
      if (req.method === 'GET' && suffix === '' && !url.search) return send(res, 200, { id: 900 + iid, iid, project_id: state.mode === 'wrong-target' ? target + 1 : target, target_project_id: target, source_project_id: state.mode === 'bad-source' ? String(source) : source, state: 'opened', diff_refs: { head_sha: head, start_sha: refs.start, base_sha: refs.merge_base }, changes_count: '1', title: 'Pinned return-value change', description: 'Synthetic consumed contract', author: { username: 'fixture-author' }, source_branch: 'feature', target_branch: 'main', labels: ['ai-review'], draft: false });
      if (req.method === 'GET' && suffix === 'versions' && [...url.searchParams.keys()].every(k => ['page', 'per_page'].includes(k))) return send(res, 200, [{ id: state.head2 ? 10 : 9, ...triple }], { 'X-Next-Page': '' });
      if (req.method === 'GET' && suffix === `versions/${state.head2 ? 10 : 9}` && !url.search) return send(res, 200, { id: state.head2 ? 10 : 9, ...triple, state: 'collected', real_size: '1', diffs: [{ old_path: manifest.paths.old, new_path: manifest.paths.new, new_file: false, deleted_file: false, renamed_file: true, diff: manifest.diffs[state.head2 ? 'next' : 'first'] }] });
      if (req.method === 'GET' && suffix === 'commits') return send(res, 200, [{ id: '6'.repeat(40), message: 'Change explicit return result' }], { 'X-Next-Page': '' });
      if (suffix === 'discussions') {
        const items = state.discussions[iid] || [];
        if (req.method === 'GET' && [...url.searchParams.keys()].every(k => ['page', 'per_page'].includes(k))) {
          const page = url.searchParams.get('page') || '1';
          if (page === '1') return send(res, 200, [{ id: 'unrelated', notes: [{ id: 90, body: 'Existing unrelated note' }] }], { 'X-Next-Page': '2' });
          if (page === '2') return send(res, 200, items, { 'X-Next-Page': '' });
        }
        if (req.method === 'POST' && !url.search) {
          const body = await read(req);
          if (typeof body.body !== 'string' || !body.body.includes('<!-- ai-review findings_hash=')) return send(res, 400, { error: 'marker required' });
          const inline = Boolean(body.position);
          if (inline && (body.position.new_path !== manifest.paths.new || body.position.new_line !== 2 || body.position.head_sha !== head || body.position.start_sha !== refs.start || body.position.base_sha !== refs.merge_base)) return send(res, 400, { error: 'position mismatch' });
          const key = `${iid}:${inline ? 'inline' : 'summary'}`;
          state.attempts[key] = (state.attempts[key] || 0) + 1;
          const fault = state.faults[String(iid)];
          if (!inline && fault && state.attempts[key] === 1) return send(res, 500, { error: 'synthetic summary failure' }, fault === 'retire' ? { 'X-Proof-Gate': 'retire' } : {});
          const id = 1000 + Object.values(state.discussions).reduce((count, rows) => count + rows.length, 0);
          const item = { id: String(id), notes: [{ id, body: body.body, ...(inline ? { position: body.position } : {}) }] };
          state.discussions[iid] = [...items, item];
          return send(res, 201, item);
        }
      }
    }
    const repo = new RegExp(`^/api/v4/projects/(${target}|${source})/repository/(.*)$`).exec(url.pathname);
    if (repo) {
      const project = Number(repo[1]), part = repo[2], revision = url.searchParams.get('ref');
      const sideValid = revision === refs.start || revision === refs.merge_base ? project === target : revision === head && project === source;
      if (req.method === 'GET' && part === 'tree') {
        if (!sideValid || [...url.searchParams.keys()].some(k => !['ref', 'path', 'recursive', 'per_page', 'pagination', 'page_token'].includes(k)) || url.searchParams.get('per_page') !== '100' || url.searchParams.get('pagination') !== 'keyset') return send(res, 400, { error: 'tree scope' });
        const recursive = url.searchParams.get('recursive') === 'true';
        if (recursive && ['keyset', 'incomplete-list'].includes(state.mode)) {
          if (url.searchParams.get('page_token') === 'next') return send(res, 200, [entry(manifest.paths.new, state.head2 ? 'new2' : 'new')]);
          const dirs = Array.from({ length: 100 }, (_, n) => ({ id: n.toString(16).padStart(40, '0'), name: `dir${n}`, path: `dir${n}`, type: 'tree', mode: '040000' }));
          return send(res, 200, dirs, state.mode === 'keyset' ? { Link: `<${tenant.origin}${url.pathname}?${new URLSearchParams([...url.searchParams, ['page_token', 'next']])}>; rel="next"` } : {});
        }
        return send(res, 200, tree(revision, state, recursive, url.searchParams.get('path') || ''));
      }
      if (req.method === 'HEAD' && part.startsWith('files/') && sideValid && [...url.searchParams.keys()].join(',') === 'ref') {
        const path = decodeURIComponent(part.slice(6));
        const blob = knownBlob(revision, path, state);
        if (!blob) return send(res, 404, { error: 'unknown file' });
        if (state.mode === 'incomplete-size' && path === manifest.paths.unknown) return send(res, 503, { error: 'synthetic size failure' });
        res.writeHead(200, { 'X-Gitlab-Blob-Id': blob.oid, 'X-Gitlab-Size': String(blob.size), 'X-Gitlab-Ref': revision, 'Content-Length': '0' }); return res.end();
      }
      const raw = /^blobs\/([a-f0-9]{40})\/raw$/.exec(part);
      if (req.method === 'GET' && raw && !url.search) {
        const entries = project === target ? ['old', 'javascript', 'unknown', 'empty', 'agents', 'rules'] : [state.head2 ? 'new2' : 'new', 'javascript', 'unknown', 'empty', 'agents', 'rules'];
        const blob = entries.map(k => manifest.blobs[k]).find(item => item.oid === raw[1]);
        if (!blob) return send(res, 404, { error: 'unknown proved blob' });
        const bytes = Buffer.from(blob.text); res.writeHead(200, { 'Content-Type': 'application/octet-stream', 'Content-Length': bytes.length }); return res.end(bytes);
      }
    }
    return send(res, 501, { error: 'unmodelled route or method' });
  } catch (_) { if (!res.headersSent) send(res, 400, { error: 'bounded fixture request rejected' }); else res.destroy(); }
});
server.requestTimeout = 10000; server.headersTimeout = 10000;
server.listen(Number(process.env.PORT), '127.0.0.1');
