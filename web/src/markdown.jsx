/**
 * 极简 Markdown 渲染器（零依赖，输出 React 元素）。
 *
 * **为什么自己写而不是引 react-markdown**
 *
 * 实测 300 条真实 agent 消息，只用到三种语法：
 *   bold 5% · bullet 5% · ordered 4%（heading/code/table/link 均为 0%）
 * 引一个几十 KB 的完整 Markdown 解析器（还带 remark/rehype 一串依赖）
 * 只为渲染 `**粗体**` 和 `- 列表` 不划算。这里 200 行覆盖实际用到的子集。
 *
 * **为什么用 React 元素而不是 dangerouslySetInnerHTML**
 *
 * LLM 输出是**不可信内容**（可能被用户诱导注入 HTML）。构建 React 元素
 * 时文本自动转义，注入面为零；用 innerHTML 就必须再引一个 sanitizer。
 * 这是本文件最重要的一条约束——**永远不要改成 innerHTML**。
 *
 * **流式容错**
 *
 * 流式时文本是逐段到达的，`**领取渠道` 这种"标记还没闭合"的中间态很常见。
 * 若原样渲染，用户会看到一闪而过的裸星号。所以 `streaming` 为真时，
 * 会剥掉末尾未闭合的标记，等它闭合后自然变成粗体。
 */

import { Fragment, useMemo } from "react";

// ---- 块级语法 ----
const RE_UL = /^\s*[-*+]\s+(.*)$/;
const RE_OL = /^\s*(\d+)[.)]\s+(.*)$/;
const RE_HEADING = /^(#{1,6})\s+(.*)$/;
const RE_QUOTE = /^>\s?(.*)$/;
const RE_HR = /^\s*([-*_])\1{2,}\s*$/;
// 整行就是粗体：`**领取渠道**`。LLM 用它当小标题，单独给样式更好读
const RE_LABEL = /^\*\*(.+?)\*\*$/;

/**
 * 剥掉流式文本里**未配对**的行内标记。
 *
 * 流式时文本逐段到达，中间态很常见：
 *   `**领取`      ← 开了粗体还没闭合
 *   `**领取渠道`  ← 同上（注意标记在**开头**，不在末尾）
 *   `` `search_ ``  ← 开了行内代码还没闭合
 * 若原样渲染，用户会看到一闪而过的裸星号/反引号。
 *
 * **为什么不能只处理末尾**：最初实现只看结尾是不是 `**`，
 * 但真实中间态是"开头有、结尾没有"，裸标记照样会闪。
 * 正确做法是数出现次数：奇数次说明最后一个没闭合，去掉那一个。
 *
 * 只处理 `**` 与 `` ` ``：实测 300 条真实消息里单星号斜体出现 0 次，
 * 为它增加复杂度不值得（未闭合时最多闪一个 `*`，影响可忽略）。
 */
function stripUnclosedMarkers(text) {
  let out = text;

  // 粗体：`**` 出现奇数次 → 最后一个未闭合，去掉它
  const bolds = out.match(/\*\*/g);
  if (bolds && bolds.length % 2 === 1) {
    const idx = out.lastIndexOf("**");
    out = out.slice(0, idx) + out.slice(idx + 2);
  }

  // 行内代码：反引号奇数次 → 去掉最后一个
  const ticks = out.match(/`/g);
  if (ticks && ticks.length % 2 === 1) {
    const idx = out.lastIndexOf("`");
    out = out.slice(0, idx) + out.slice(idx + 1);
  }

  // 未闭合的链接：`[文字](部分url` 或 `[文字` → 先只显示文字
  const openLink = out.match(/\[([^\]]*)\]\([^)]*$/);
  if (openLink) {
    out = out.slice(0, out.length - openLink[0].length) + openLink[1];
  } else {
    const openBracket = out.match(/\[([^\]]*)$/);
    if (openBracket) {
      out = out.slice(0, out.length - openBracket[0].length) + openBracket[1];
    }
  }

  return out;
}

/**
 * 行内解析：**粗体** / *斜体* / `代码` / [链接](url)。
 *
 * 为什么手写扫描而不是一个大正则：`*斜体*` 与 `**粗体**` 共享前缀字符，
 * 大正则靠回溯区分容易在边界（`2 * 3`、`a**b`）误判。逐位置按优先级尝试
 * 匹配，行为可预测、可单测。
 */
function parseInline(text, keyPrefix) {
  const out = [];
  let buf = "";
  let i = 0;
  let seq = 0;

  const flush = () => {
    if (buf) {
      out.push(buf);
      buf = "";
    }
  };

  while (i < text.length) {
    const ch = text[i];

    // `code`
    if (ch === "`") {
      const end = text.indexOf("`", i + 1);
      if (end > i + 1) {
        flush();
        out.push(<code key={`${keyPrefix}-c${seq++}`}>{text.slice(i + 1, end)}</code>);
        i = end + 1;
        continue;
      }
    }

    // **bold** / __bold__
    if ((ch === "*" || ch === "_") && text[i + 1] === ch) {
      const marker = ch + ch;
      const end = text.indexOf(marker, i + 2);
      // 内容不能以空白开头/结尾（避免把 `2 ** 3` 当粗体）
      if (end > i + 2 && !/\s/.test(text[i + 2]) && !/\s/.test(text[end - 1])) {
        flush();
        out.push(
          <strong key={`${keyPrefix}-b${seq++}`}>
            {parseInline(text.slice(i + 2, end), `${keyPrefix}-b${seq}`)}
          </strong>,
        );
        i = end + 2;
        continue;
      }
    }

    // *italic* / _italic_
    if (ch === "*" || ch === "_") {
      const end = text.indexOf(ch, i + 1);
      if (end > i + 1 && !/\s/.test(text[i + 1]) && !/\s/.test(text[end - 1])) {
        flush();
        out.push(
          <em key={`${keyPrefix}-i${seq++}`}>
            {parseInline(text.slice(i + 1, end), `${keyPrefix}-i${seq}`)}
          </em>,
        );
        i = end + 1;
        continue;
      }
    }

    // [文字](url)
    if (ch === "[") {
      const m = text.slice(i).match(/^\[([^\]]+)\]\(([^)\s]+)\)/);
      if (m) {
        flush();
        // 只放行安全协议：LLM 可能被诱导产出 javascript: 链接
        const safe = /^(https?:|mailto:|\/)/i.test(m[2]);
        out.push(
          safe ? (
            <a key={`${keyPrefix}-a${seq++}`} href={m[2]} target="_blank" rel="noopener noreferrer">
              {m[1]}
            </a>
          ) : (
            <Fragment key={`${keyPrefix}-a${seq++}`}>{m[1]}</Fragment>
          ),
        );
        i += m[0].length;
        continue;
      }
    }

    buf += ch;
    i += 1;
  }

  flush();
  return out;
}

/** 行类型判定：分组的唯一依据 */
function lineKind(line) {
  if (RE_HR.test(line)) return "hr";
  if (RE_HEADING.test(line)) return "heading";
  if (RE_QUOTE.test(line)) return "quote";
  if (RE_UL.test(line)) return "ul";
  if (RE_OL.test(line)) return "ol";
  return "plain";
}

/**
 * 把一个"块"（同类行）解析成 React 元素。
 *
 * @param kind 该块的行类型（由 lineKind 判定）
 */
function parseBlock(kind, lines, keyPrefix, trailing) {
  const first = lines[0];

  if (kind === "hr") return <hr key={keyPrefix} />;

  if (kind === "heading") {
    const m = first.match(RE_HEADING);
    const Tag = `h${Math.min(m[1].length + 2, 6)}`;   // 气泡里不用 h1/h2，避免压过页面层级
    return (
      <Tag key={keyPrefix} className="md-heading">
        {parseInline(m[2], `${keyPrefix}-h`)}
        {trailing}
      </Tag>
    );
  }

  if (kind === "quote") {
    const text = lines.map((l) => l.replace(RE_QUOTE, "$1")).join("\n");
    return (
      <blockquote key={keyPrefix} className="md-quote">
        {parseInline(text, `${keyPrefix}-q`)}
        {trailing}
      </blockquote>
    );
  }

  if (kind === "ul" || kind === "ol") {
    const re = kind === "ul" ? RE_UL : RE_OL;
    const items = [];
    for (const line of lines) {
      const m = line.match(re);
      if (m) {
        // ul 的正文在 group1，ol 的正文在 group2（group1 是序号）
        items.push(kind === "ul" ? m[1] : m[2]);
      } else if (items.length) {
        // 缩进的续行：并回上一项（LLM 偶尔把长条目折行）
        items[items.length - 1] += " " + line.trim();
      }
    }
    const Tag = kind === "ul" ? "ul" : "ol";
    const start = kind === "ol" ? Number(first.match(RE_OL)[1]) : undefined;
    return (
      <Tag key={keyPrefix} className={kind === "ul" ? "md-ul" : "md-ol"} start={start}>
        {items.map((it, idx) => (
          <li key={idx}>
            {parseInline(it, `${keyPrefix}-${kind}${idx}`)}
            {/* 流式光标挂在最后一项末尾，而不是跑到列表下面单独一行 */}
            {idx === items.length - 1 ? trailing : null}
          </li>
        ))}
      </Tag>
    );
  }

  // 单行且整行粗体 → 小标题样式。
  // LLM 的惯用写法是 `**怎么用**` 后面紧跟 `- 列表`（**中间没有空行**），
  // 所以必须按行分组才能把它单独识别出来——按空行分块会把整段吞进段落。
  const label = lines.length === 1 && first.match(RE_LABEL);
  if (label) {
    return (
      <p key={keyPrefix} className="md-label">
        {parseInline(label[1], `${keyPrefix}-l`)}
        {trailing}
      </p>
    );
  }

  // 普通段落：单换行 = 换行（LLM 常这样分行，不用空行）
  return (
    <p key={keyPrefix} className="md-p">
      {lines.map((line, idx) => (
        <Fragment key={idx}>
          {idx > 0 && <br />}
          {parseInline(line, `${keyPrefix}-p${idx}`)}
        </Fragment>
      ))}
      {trailing}
    </p>
  );
}

/**
 * 渲染消息正文。
 *
 * @param text      原始文本（可能含 Markdown）
 * @param streaming 是否正在流式接收（为真时剥掉末尾未闭合标记）
 * @param trailing  追加到最后一个文本块末尾的节点（流式光标）
 */
function renderMarkdown(text, { streaming = false, trailing = null } = {}) {
  if (!text) return trailing ? <p className="md-p">{trailing}</p> : null;

  const source = streaming ? stripUnclosedMarkers(text) : text;
  const lines = source.split(/\r?\n/);

  // ---- 按行类型分组 ----
  //
  // 为什么不能"按空行切块"：实测 LLM 输出里 `**小标题**` 与紧跟的 `- 列表`
  // 之间**没有空行**，按空行切会把它们并进同一个块，只能按第一行判断类型，
  // 于是整个块退化成段落（列表与粗体全部原样显示）。
  // 逐行分类、类型变化就断开，才能正确识别。
  const groups = [];
  let current = null;

  const push = (kind, line) => {
    if (!current || current.kind !== kind) {
      if (current) groups.push(current);
      current = { kind, lines: [] };
    }
    current.lines.push(line);
  };

  for (const line of lines) {
    if (!line.trim()) {
      if (current) groups.push(current);
      current = null;
      continue;
    }
    const kind = lineKind(line);

    // 分隔线 / 标题总是独占一块
    if (kind === "hr" || kind === "heading") {
      if (current) groups.push(current);
      groups.push({ kind, lines: [line] });
      current = null;
      continue;
    }

    // 缩进续行（2 空格以上且不是列表标记）并入当前列表
    if (current && (current.kind === "ul" || current.kind === "ol") && /^\s{2,}\S/.test(line)) {
      current.lines.push(line);
      continue;
    }

    push(kind, line);
  }
  if (current) groups.push(current);

  return groups.map((g, idx) => {
    const isLast = idx === groups.length - 1;
    return parseBlock(g.kind, g.lines, `blk${idx}`, isLast ? trailing : null);
  });
}

/** 带缓存的组件包装：流式时每次 delta 都会重渲染，useMemo 省掉重复解析 */
export function Markdown({ text, streaming, trailing }) {
  const nodes = useMemo(
    () => renderMarkdown(text, { streaming, trailing }),
    [text, streaming, trailing],
  );
  return <>{nodes}</>;
}
