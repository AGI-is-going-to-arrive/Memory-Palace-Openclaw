import { normalizeText } from "./utils.js";

const PROMPT_INJECTION_PATTERNS = [
  /\bignore\b.{0,40}\b(all|any|previous|above|prior)\b.{0,20}\binstructions?\b/iu,
  /do not follow (the )?(system|developer)/iu,
  /\b(act as|pretend (?:to be|you are)|roleplay as)\b.{0,40}\b(system|developer|assistant|tool)\b/iu,
  /\b(reveal|show|output|print|dump)\b.{0,40}\b(system prompt|developer message|hidden instructions?)\b/iu,
  /system prompt/iu,
  /developer message/iu,
  /(?:忽略|不要遵循).{0,20}(?:系统|开发者|先前|之前|以上).{0,20}(?:指令|提示|消息)/u,
  /(?:输出|显示|泄露).{0,20}(?:系统提示|系统提示词|developer message|system prompt|隐藏指令)/iu,
  /(?:扮演|假装你是).{0,20}(?:系统|开发者|助手|工具)/u,
  /(?:以前|前|上記|すべて).{0,20}(?:指示|命令|プロンプト|メッセージ).{0,20}(?:無視|従(?:うな|わない))/u,
  /(?:表示|出力|漏[え洩]|公開).{0,20}(?:システムプロンプト|開発者メッセージ|隠された指示|システム指示)/u,
  /(?:이전|앞선|위|시스템|개발자).{0,20}(?:지시|명령|프롬프트|메시지).{0,20}(?:무시|따르지\s*마)/u,
  /(?:출력|표시|유출|공개).{0,20}(?:시스템\s*프롬프트|개발자\s*메시지|숨겨진\s*지침)/u,
  /<\s*(system|assistant|developer|tool|function|memory-palace-profile|memory-palace-recall|memory-palace-reflection|memory-palace-host-bridge)\b/iu,
] as const;
const PROMPT_INJECTION_COMPACT_PATTERNS = [
  /ignore(?:all|any|previous|above|prior).*instructions?/iu,
  /donotfollow(?:the)?(?:system|developer)/iu,
  /(?:忽略|不要遵循).*(?:系统|开发者|先前|之前|以上).*(?:指令|提示|消息)/u,
  /(?:输出|显示|泄露).*(?:系统提示|系统提示词|developermessage|systemprompt|隐藏指令)/iu,
  /(?:以前|前|上記|すべて).*(?:指示|命令|プロンプト|メッセージ).*(?:無視|従(?:うな|わない))/u,
  /(?:表示|出力|漏[え洩]|公開).*(?:システムプロンプト|開発者メッセージ|隠された指示|システム指示)/u,
  /(?:이전|앞선|위|시스템|개발자).*(?:지시|명령|프롬프트|메시지).*(?:무시|따르지마|따르지\s*마)/u,
  /(?:출력|표시|유출|공개).*(?:시스템프롬프트|개발자메시지|숨겨진지침)/u,
  /systemprompt/iu,
  /developermessage/iu,
] as const;

const PROMPT_INJECTION_ZERO_WIDTH_PATTERN = /[\u200B-\u200D\u2060\uFEFF]/gu;
const PROMPT_INJECTION_COMBINING_MARK_PATTERN = /\p{M}+/gu;
const PROMPT_INJECTION_CONFUSABLE_PATTERN =
  /[013457@\u0430\u0435\u043e\u0440\u0441\u0443\u0456\u0458\u217c]/gu;
const PROMPT_INJECTION_CONFUSABLE_MAP: Record<string, string> = {
  "0": "o",
  "1": "i",
  "3": "e",
  "4": "a",
  "5": "s",
  "7": "t",
  "@": "a",
  "\u0430": "a",
  "\u0435": "e",
  "\u043e": "o",
  "\u0440": "p",
  "\u0441": "c",
  "\u0443": "y",
  "\u0456": "i",
  "\u0458": "j",
  "\u217c": "l",
};
const PROMPT_ESCAPE_MAP: Record<string, string> = {
  "&": "&amp;",
  "<": "&lt;",
  ">": "&gt;",
  '"': "&quot;",
  "'": "&#39;",
};

export function normalizePromptInjectionText(text: string): string {
  return normalizeText(
    text
      .normalize("NFKD")
      .replace(PROMPT_INJECTION_ZERO_WIDTH_PATTERN, " ")
      .replace(PROMPT_INJECTION_COMBINING_MARK_PATTERN, "")
      .replace(
        PROMPT_INJECTION_CONFUSABLE_PATTERN,
        (character) => PROMPT_INJECTION_CONFUSABLE_MAP[character] ?? character,
      )
      .normalize("NFC"),
  );
}

export function looksLikePromptInjection(text: string): boolean {
  const normalized = normalizePromptInjectionText(text);
  if (!normalized) {
    return false;
  }
  const compacted = normalized.replace(/\s+/g, "");
  return (
    PROMPT_INJECTION_PATTERNS.some((pattern) => pattern.test(normalized)) ||
    PROMPT_INJECTION_COMPACT_PATTERNS.some((pattern) => pattern.test(compacted))
  );
}

export function escapeMemoryForPrompt(text: string): string {
  return text.replace(/[&<>"']/g, (char) => PROMPT_ESCAPE_MAP[char] ?? char);
}
