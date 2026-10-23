/**
 * 消息列表的合并规则（双端共用）。
 *
 * 为什么需要：一条消息可能经**多条路径**到达同一个界面——
 *   1. REST 响应体（发起方的 ack）
 *   2. WebSocket 推送（服务端广播给所有 watcher，包含发起方自己）
 *   3. 重连后的 ready 全量快照
 * 任何两路都直接 push 就会出现重复气泡，而刷新后又变回一条（从 DB 重载），
 * 是"刷新一下就好了"这类最难排查的 bug 的典型来源。
 */

/**
 * 把一条消息并入列表，按服务端主键去重。
 *
 * 为什么按 id 而不是按内容去重：本地时间戳与服务端 created_at 必然不同，
 * 而内容匹配遇到"连发两条一样的话"会误杀。主键 id 是唯一天然去重键。
 * id 为 null（未落库的消息，如排队确认话术）不参与去重，直接追加。
 *
 * 同 id 已存在时用新值覆盖字段（服务端版本的时间戳/昵称更权威），
 * 但**保留原位置**——否则消息会因重连补发而跳到列表末尾。
 */
export function mergeMessage(list, incoming) {
  if (incoming?.id != null) {
    const i = list.findIndex((m) => m.id === incoming.id);
    if (i !== -1) {
      const next = list.slice();
      next[i] = { ...next[i], ...incoming };
      return next;
    }
  }
  return [...list, incoming];
}

/**
 * 合并一批消息（用于 ready 快照 / 历史接口的增量对齐）。
 * 快照通常是全量替换，这个函数留给"不能直接替换"的场景使用。
 */
export function mergeMessages(list, incomingList) {
  return (incomingList ?? []).reduce((acc, m) => mergeMessage(acc, m), list);
}
