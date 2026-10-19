import { ROLE } from "../constants";

/**
 * 消息行的展示配置：按角色给出头像文字与角色标签。
 *
 * 气泡配色类名各端视角不同（如用户在用户端是蓝底、在坐席端是灰底），
 * 由调用方通过 bubbleClass 指定；头像/标签双端共用，保证角色识别一致。
 */
const ROLE_DISPLAY = {
  [ROLE.USER]: { avatar: "cust", avatarText: "用", tag: "客户" },
  [ROLE.AGENT]: { avatar: "bot2", avatarText: "智", tag: "智能客服" },
  [ROLE.HUMAN]: { avatar: "human", avatarText: "客", tag: "人工" },
};

export function roleDisplay(role) {
  return ROLE_DISPLAY[role] ?? ROLE_DISPLAY[ROLE.AGENT];
}

/**
 * 坐席端视角的气泡类名映射：
 * 客户浅灰蓝 / 智能客服白底蓝描边 / 人工坐席绿色（与用户端一致）。
 */
const AGENT_VIEW_BUBBLE = {
  [ROLE.USER]: "cust",
  [ROLE.AGENT]: "agent-view",
  [ROLE.HUMAN]: "human",
};

export function agentViewBubbleClass(role) {
  return AGENT_VIEW_BUBBLE[role] ?? AGENT_VIEW_BUBBLE[ROLE.AGENT];
}

/**
 * 渲染一条消息（头像 + 气泡），不含行容器与对齐方向——那由各端视角决定。
 *
 * @param message  {role, content}
 * @param bubbleClass 该端视角下此消息的气泡类名（如 "user" / "cust" / "agent-view"）
 * @param withAvatar  是否渲染头像（用户看自己的消息通常不需要）
 * @param showTag     是否显示角色标签（自己的消息一般不需要标明身份）
 */
// 时间展示：今天的消息只显示时分，更早的带日期
function formatTime(ts) {
  if (!ts) return "";
  const d = new Date(ts);
  const now = new Date();
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  if (d.toDateString() === now.toDateString()) return hm;
  return `${d.getMonth() + 1}/${d.getDate()} ${hm}`;
}

export function MessageBubble({ message, bubbleClass, withAvatar = true, showTag = true, showTime }) {
  if (!message.content) return null; // 空消息不渲染（如坐席接入后的无应答轮次）
  const d = roleDisplay(message.role);
  // 人工消息且带坐席昵称：标签显示具体是谁回复的（坐席端视角保持"人工"）
  const isHumanNamed = message.role === ROLE.HUMAN && message.sender_name;
  const time = formatTime(message.ts);
  // 时间默认跟随角色标签展示；隐藏标签的消息（如用户自己的气泡）
  // 需单独开 showTime 才能显示发送时间
  const wantTime = showTime ?? showTag;
  return (
    <>
      {withAvatar && (
        <span className={`avatar bot ${d.avatar}`} aria-hidden="true">
          {d.avatarText}
        </span>
      )}
      <div className={`msg ${bubbleClass}`}>
        {/* 无标签但要显示时间的消息：时间在气泡内、消息上方独立一行 */}
        {!showTag && wantTime && time && (
          <time className="msg-time-solo">{time}</time>
        )}
        {showTag && (
          <span className="role-tag">
            {isHumanNamed ? message.sender_name : d.tag}
            {wantTime && time && <time className="msg-time">{time}</time>}
          </span>
        )}
        {message.content}
      </div>
    </>
  );
}
