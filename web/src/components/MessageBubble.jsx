import { ROLE } from "../constants";

/**
 * 一条消息 = 元信息行（角色标签 + 时间）+ 内容块。
 *
 * 这一版刻意去掉了头像和彩色气泡：
 * - 头像用汉字圆角块（"智""客""用"）是 AI 聊天界面的默认长相，信息量为零
 * - 四色气泡靠颜色区分角色，等于让颜色承担语义——色弱用户直接失去信息
 *
 * 现在角色由**文字标签**表达（客户 / 智能客服 / 人工），颜色只做加速识别；
 * 内容块统一白底描边，左侧 2px 色条按角色变色。
 */

// 角色标签文案：同一角色在两端视角下叫法不同（用户看自己是"你"，坐席看是"客户"）
const ROLE_LABEL = {
  [ROLE.USER]: "客户",
  [ROLE.AGENT]: "智能客服",
  [ROLE.HUMAN]: "人工坐席",
};

// 标签的语义修饰类：只影响左侧色条与标签颜色，不影响内容可读性
const ROLE_CLASS = {
  [ROLE.USER]: "line--user",
  [ROLE.AGENT]: "line--agent",
  [ROLE.HUMAN]: "line--human",
};

// 时间展示：今天的只显示时分，更早的带日期。
// 等宽数字 + 补零，保证一列时间在视觉上对齐
function formatTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  const now = new Date();
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (d.toDateString() === now.toDateString()) return hm;
  return `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}

/**
 * @param message  {role, content, ts, sender_name}
 * @param side     "end"（自己，右对齐）| "start"（对方，左对齐）
 * @param label    角色标签文案覆盖（如用户端把自己的消息标成"你"）
 * @param old      是否旧会话消息（坐席端历史流里非当前 thread 的记录）
 */
export function MessageBubble({ message, side = "start", label, old = false }) {
  if (!message.content) return null;   // 空消息不渲染（坐席接入后的无应答轮次）

  // 人工消息带坐席昵称时，标签显示"具体是谁"而不是笼统的"人工坐席"
  const roleLabel =
    label ??
    (message.role === ROLE.HUMAN && message.sender_name
      ? message.sender_name
      : ROLE_LABEL[message.role] ?? ROLE_LABEL[ROLE.AGENT]);
  const time = formatTime(message.ts);

  return (
    <div
      className={`line line--${side} ${ROLE_CLASS[message.role] ?? ""} ${old ? "line--old" : ""}`}
    >
      <div className="line-meta">
        <span className={`role role--${message.role}`}>{roleLabel}</span>
        {time && <time className="mono" dateTime={message.ts}>{time}</time>}
        {old && <span className="mono">历史</span>}
      </div>
      <div className="bubble">{message.content}</div>
    </div>
  );
}

export default MessageBubble;
