/**
 * cf-optimizer 结果中转
 *
 * 作用：接收 NAS 上容器推送的优选结果，存进 KV，再以纯文本形式对外提供，
 *      供 CFNext 面板的「自定义订阅 / 优选节点」填 URL 拉取。
 *
 * 部署：
 *   1. Cloudflare Dashboard -> Workers 和 Pages -> 创建 Worker，粘贴本文件
 *   2. 设置 -> 变量 -> 添加 KV 命名空间绑定：变量名 IPS
 *   3. 设置 -> 变量 -> 添加文本变量 UPLOAD_TOKEN = 一串你自己定的长随机串
 *   4. 部署，记下地址，例如 https://cf-ips.你的子域.workers.dev
 *
 * 用法：
 *   POST /upload    body 为纯文本，每行一条 IP:端口#标签，请求头 x-token 必须正确
 *   GET  /ips.txt   返回最新列表（CFNext 填这个地址）
 *   GET  /status    返回更新时间和条数
 */

const MAX_LINES = 200;
const MAX_BYTES = 64 * 1024;

function text(body, status = 200, extra = {}) {
  return new Response(body, {
    status,
    headers: {
      'content-type': 'text/plain; charset=utf-8',
      'cache-control': 'no-store',
      ...extra,
    },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, '') || '/';

    if (request.method === 'POST' && path === '/upload') {
      const token = env.UPLOAD_TOKEN || '';
      if (!token) return text('server not configured: UPLOAD_TOKEN missing', 500);
      if ((request.headers.get('x-token') || '') !== token) return text('forbidden', 403);

      const raw = await request.text();
      if (raw.length > MAX_BYTES) return text('payload too large', 413);

      const lines = raw
        .split(/\r?\n/)
        .map((s) => s.trim())
        .filter((s) => s && !s.startsWith('#'))
        .slice(0, MAX_LINES);

      if (lines.length === 0) return text('empty payload', 400);

      await Promise.all([
        env.IPS.put('list', lines.join('\n')),
        env.IPS.put('updated', new Date().toISOString()),
        env.IPS.put('count', String(lines.length)),
      ]);

      return text(`ok ${lines.length}\n`);
    }

    if (request.method === 'GET' && (path === '/ips.txt' || path === '/')) {
      const [list, updated] = await Promise.all([env.IPS.get('list'), env.IPS.get('updated')]);
      return text(list || '', 200, { 'x-updated': updated || 'never' });
    }

    if (request.method === 'GET' && path === '/status') {
      const [updated, count] = await Promise.all([env.IPS.get('updated'), env.IPS.get('count')]);
      return Response.json({ updated: updated || null, count: Number(count || 0) });
    }

    return text('not found', 404);
  },
};
