// Local OpenAI-compatible envelope consumed by the real shared gateway; no real model.
'use strict';
const http = require('http'); const fs = require('fs');
const config = JSON.parse(fs.readFileSync(process.env.T15_PRIVATE_CONFIG, 'utf8'));
const manifest = JSON.parse(fs.readFileSync(__dirname + '/manifest.json', 'utf8'));
const calls = [];
const server = http.createServer(async (req, res) => {
  const send = (status, data) => { const bytes = Buffer.from(JSON.stringify(data)); res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': bytes.length }); res.end(bytes); };
  try {
    if (req.url === '/__control' && req.method === 'POST' && req.headers['x-proof-control'] === config.control_key) { req.resume(); return send(200, { calls }); }
    if (req.url !== '/v1/chat/completions' || req.method !== 'POST') return send(501, { error: 'unmodelled model route' });
    if (req.headers.authorization !== `Bearer ${config.model_key}`) return send(403, { error: 'model credential scope' });
    let size = 0; const chunks = [];
    for await (const chunk of req) { size += chunk.length; if (size > 1048576) throw new Error('body cap'); chunks.push(chunk); }
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    const task = body.response_format?.json_schema?.name;
    if (body.model !== 'test-model' || !['RepoConventionsDraft', 'ReviewOutput'].includes(task)) return send(501, { error: 'unmodelled structured task' });
    calls.push({ task, prompt_tokens: 1200, completion_tokens: 300 });
    return send(200, { model: 'test-model', choices: [{ message: { content: JSON.stringify(task === 'RepoConventionsDraft' ? manifest.conventions : manifest.review) }, finish_reason: 'stop' }], usage: { prompt_tokens: 1200, completion_tokens: 300 } });
  } catch (_) { return send(400, { error: 'bounded model request rejected' }); }
});
server.requestTimeout = 10000; server.headersTimeout = 10000;
server.listen(Number(process.env.PORT), '127.0.0.1');
