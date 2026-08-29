'use client';

import type { FormEvent } from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AgentApiError, chatWithAgent, createAsset, createBlogger, createIdempotencyKey, listBloggers, listScriptOutputs, requestJson, reviseScriptOutput, retryScriptOutput, updateBlogger } from './lib/agent-api';

type LibraryType = 'knowledge' | 'material' | 'algorithm';
type Drawer = 'library' | 'health' | 'report' | null;
type ProfileEditorMode = 'create' | 'edit';
type Busy = 'load' | 'save' | 'build' | 'assess' | 'generate' | null;
type SessionStatus = 'idle' | 'collecting' | 'confirming';
type ScriptEditField = 'title' | 'hook' | 'body' | 'ending' | 'tags';

type Blogger = {
  id: number;
  name: string;
  platform: string;
  content_types: string[];
  style: string;
  follower_band: string;
  monetization_types: string[];
  routes?: string | null;
  viral_topic?: string | null;
  frequency?: string | null;
  profile_state: string;
};

type ProfileDraft = {
  name: string;
  platform: string;
  content_types: string[];
  style: string;
  follower_band: string;
  monetization_types: string[];
  routes: string;
  viral_topic: string;
  frequency: string;
};

type ProfileSessionReply = {
  session_id: number;
  status: 'collecting' | 'confirming' | 'completed';
  question: string | null;
  collected_profile: Partial<ProfileDraft>;
};

type Asset = {
  id: number;
  lib_type: LibraryType;
  category: string;
  title: string;
  content: string;
  tags: string[];
  source_type: string;
  credibility: number;
  sources: { title?: string | null; source_title?: string | null; publisher?: string | null; url?: string | null }[];
};

type AssessmentIndicator = {
  id: number;
  name: string;
  meaning: string;
  business_meaning: string;
  weight: number;
  weight_reason: string;
  score: number;
  reason: string;
  evidence: { asset_id?: number; claim?: string }[];
};

type Assessment = {
  id: number;
  status: string;
  overall_score: number | null;
  summary: string | null;
  feature_readiness: unknown;
  suggestions: unknown;
  indicators: AssessmentIndicator[];
  error_message?: string | null;
};

type BuildRun = { id: number; status: string; output_summary?: Record<string, unknown> | null; error_message?: string | null };
type OutputAsset = { asset_id: number; usage_type: string; claim: string };
type Output = {
  id: number;
  title: string;
  status: string;
  assessment_id: number | null;
  content_json: unknown;
  assets: OutputAsset[];
  error_code?: string | null;
  error_message?: string | null;
};

type GeneratedScript = {
  title: string;
  hook: string;
  body: string;
  ending: string;
  tags: string[];
  sources: string[];
  outputId: number;
};

type ActionKind = 'start-profile' | 'edit-profile' | 'create-profile' | 'build' | 'assess' | 'generate' | 'open-library' | 'open-health' | 'open-report' | 'confirm-profile' | 'retry-profile' | 'confirm-material' | 'cancel-material';
type ChatAction = { label: string; kind: ActionKind; topic?: string; forceNew?: boolean; ghost?: boolean };
type ChatMessage = { id: number; role: 'assistant' | 'user'; content: string; actions?: ChatAction[]; script?: GeneratedScript };
type MaterialDraft = {
  lib_type: 'material';
  title: string;
  content: string;
  tags: string[];
  category: string;
  source_type: string;
  credibility: number;
};
type FailedProfileTurn = { sessionId: number; value: string };
type ConversationFolder = {
  id: string;
  name: string;
  bloggerId: number;
  bloggerName: string;
  createdAt: string;
  updatedAt: string;
};
type ConversationRecord = {
  id: string;
  folderId: string;
  bloggerId: number;
  bloggerName: string;
  messages: ChatMessage[];
  sessionId: number | null;
  sessionStatus: SessionStatus;
  draft: ProfileDraft;
  profileEditor: ProfileEditorMode | null;
  failedProfileTurn: FailedProfileTurn | null;
  script: GeneratedScript | null;
  currentOutput: Output | null;
  materialAwaitingContent: boolean;
  materialDraft: MaterialDraft | null;
  createdAt: string;
  updatedAt: string;
};

const DEFAULT_BLOGGER_ID = 2;
const CONVERSATION_STORAGE_KEY = 'qianyi-creator-conversations-v1';
const CONVERSATION_FOLDERS_STORAGE_KEY = 'qianyi-creator-conversation-folders-v1';

function emptyProfileDraft(): ProfileDraft {
  return { name: '', platform: '', content_types: [], style: '', follower_band: '', monetization_types: [], routes: '', viral_topic: '', frequency: '' };
}

function createConversationId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return `conversation-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function createConversation(bloggerId: number, bloggerName: string, folderId = `blogger-${bloggerId}`): ConversationRecord {
  const now = new Date().toISOString();
  return {
    id: createConversationId(),
    folderId,
    bloggerId,
    bloggerName,
    messages: [],
    sessionId: null,
    sessionStatus: 'idle',
    draft: emptyProfileDraft(),
    profileEditor: null,
    failedProfileTurn: null,
    script: null,
    currentOutput: null,
    materialAwaitingContent: false,
    materialDraft: null,
    createdAt: now,
    updatedAt: now,
  };
}

function createConversationFolder(bloggerId: number, bloggerName: string, name: string, id = createConversationId()): ConversationFolder {
  const now = new Date().toISOString();
  return { id, name: name.trim() || bloggerName || `#${bloggerId}`, bloggerId, bloggerName, createdAt: now, updatedAt: now };
}

function conversationLabel(conversation: ConversationRecord): string {
  const firstUserMessage = conversation.messages.find((message) => message.role === 'user')?.content?.trim();
  return firstUserMessage ? firstUserMessage.slice(0, 28) : '新对话';
}

type ChatIntent = 'profile' | 'material' | 'edit-script' | 'generate-script' | 'build' | 'assess' | 'casual';

function extractScriptEdit(value: string): { field: ScriptEditField; nextValue: string } | null {
  const replacement = value.match(/(?:把|将|请)?\s*(标题|开头|正文|结尾|标签)\s*(?:改为|改成|修改为|换成|替换为|设置为|调整为|重写为|：|:)\s*([\s\S]+)$/);
  if (!replacement) return null;
  const fieldMap: Record<string, ScriptEditField> = { 标题: 'title', 开头: 'hook', 正文: 'body', 结尾: 'ending', 标签: 'tags' };
  const nextValue = replacement[2].trim().replace(/^['“”\"]|['“”\"]$/g, '');
  return nextValue ? { field: fieldMap[replacement[1]], nextValue } : null;
}

function extractMaterialContent(value: string): string | null {
  const quoted = value.match(/[“「\"]([\s\S]+?)[”」\"]/);
  if (quoted?.[1]?.trim()) return quoted[1].trim();
  const explicit = value.match(/(?:素材库|素材)\s*(?:中|里)?\s*[：:]\s*([\s\S]+)$/);
  return explicit?.[1]?.trim() || null;
}

function createMaterialDraft(content: string): MaterialDraft {
  const firstLine = content.split(/\r?\n/).map((line) => line.trim()).find(Boolean) ?? content;
  return {
    lib_type: 'material',
    title: firstLine.slice(0, 40),
    content,
    tags: ['用户提供'],
    category: '用户素材',
    source_type: 'user_input',
    credibility: 1,
  };
}

function restoreConversations(): ConversationRecord[] {
  if (typeof window === 'undefined') return [];
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(CONVERSATION_STORAGE_KEY) ?? '[]');
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item): item is Record<string, unknown> => {
        if (!item || typeof item !== 'object' || Array.isArray(item)) return false;
        const record = item as Record<string, unknown>;
        return typeof record.id === 'string' && typeof record.bloggerId === 'number' && Array.isArray(record.messages);
      })
      .map((record) => ({
        ...createConversation(Number(record.bloggerId), typeof record.bloggerName === 'string' ? record.bloggerName : `#${record.bloggerId}`, typeof record.folderId === 'string' ? record.folderId : `blogger-${record.bloggerId}`),
        ...record,
        messages: record.messages as ChatMessage[],
        sessionId: typeof record.sessionId === 'number' ? record.sessionId : null,
        sessionStatus: record.sessionStatus === 'collecting' || record.sessionStatus === 'confirming' ? record.sessionStatus : 'idle',
        draft: record.draft && typeof record.draft === 'object' ? record.draft as ProfileDraft : emptyProfileDraft(),
        profileEditor: record.profileEditor === 'create' || record.profileEditor === 'edit' ? record.profileEditor : null,
        failedProfileTurn: record.failedProfileTurn && typeof record.failedProfileTurn === 'object' ? record.failedProfileTurn as FailedProfileTurn : null,
        script: record.script && typeof record.script === 'object' ? record.script as GeneratedScript : null,
        currentOutput: record.currentOutput && typeof record.currentOutput === 'object' ? record.currentOutput as Output : null,
        materialAwaitingContent: record.materialAwaitingContent === true,
        materialDraft: record.materialDraft && typeof record.materialDraft === 'object' ? record.materialDraft as MaterialDraft : null,
        createdAt: typeof record.createdAt === 'string' ? record.createdAt : new Date().toISOString(),
        updatedAt: typeof record.updatedAt === 'string' ? record.updatedAt : new Date().toISOString(),
      }));
  } catch {
    return [];
  }
}

function restoreConversationFolders(): ConversationFolder[] {
  if (typeof window === 'undefined') return [];
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(CONVERSATION_FOLDERS_STORAGE_KEY) ?? '[]');
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((item): item is Record<string, unknown> => {
      if (!item || typeof item !== 'object' || Array.isArray(item)) return false;
      const record = item as Record<string, unknown>;
      return typeof record.id === 'string' && typeof record.name === 'string' && typeof record.bloggerId === 'number';
    }).map((record) => {
      const bloggerId = Number(record.bloggerId);
      const bloggerName = typeof record.bloggerName === 'string' ? record.bloggerName : `#${bloggerId}`;
      const folder = createConversationFolder(bloggerId, bloggerName, typeof record.name === 'string' ? record.name : bloggerName, typeof record.id === 'string' ? record.id : undefined);
      return {
        ...folder,
        createdAt: typeof record.createdAt === 'string' ? record.createdAt : folder.createdAt,
        updatedAt: typeof record.updatedAt === 'string' ? record.updatedAt : folder.updatedAt,
      };
    });
  } catch {
    return [];
  }
}

function getChatIntent(value: string): ChatIntent {
  const normalized = value.trim().toLowerCase();
  if (normalized.includes('素材库') || normalized.includes('保存素材') || normalized.includes('加入素材')) return 'material';
  if (/(?:改|修改|调整|重写|替换|编辑|增加|删除|去掉).*(?:脚本|标题|开头|正文|结尾|标签|结构|内容)/.test(value)) return 'edit-script';
  if (/(?:脚本|文案|开头|标题|内容).*(?:生成|写|制作|帮我想|来一条)|(?:生成|写|制作|帮我想).*(?:脚本|文案|开头|标题|内容)/.test(value)) return 'generate-script';
  if (/(?:新增|创建|建立|开始).*(?:博主|画像|建档|采集)|(?:博主画像|用户画像|开始建档|开始采集|认识我|了解我)|(?:我叫|我是).{1,}/.test(value)) return 'profile';
  if (normalized.includes('建库') || normalized.includes('三库')) return 'build';
  if (normalized.includes('体检') || normalized.includes('健康')) return 'assess';
  return 'casual';
}

function draftFromBlogger(blogger: Blogger): ProfileDraft {
  return { name: blogger.name, platform: blogger.platform, content_types: blogger.content_types, style: blogger.style, follower_band: blogger.follower_band, monetization_types: blogger.monetization_types, routes: blogger.routes ?? '', viral_topic: blogger.viral_topic ?? '', frequency: blogger.frequency ?? '' };
}

function mergeProfile(draft: ProfileDraft, partial: Partial<ProfileDraft>): ProfileDraft {
  return {
    name: typeof partial.name === 'string' ? partial.name : draft.name,
    platform: typeof partial.platform === 'string' ? partial.platform : draft.platform,
    content_types: Array.isArray(partial.content_types) ? partial.content_types.filter((item): item is string => typeof item === 'string') : draft.content_types,
    style: typeof partial.style === 'string' ? partial.style : draft.style,
    follower_band: typeof partial.follower_band === 'string' ? partial.follower_band : draft.follower_band,
    monetization_types: Array.isArray(partial.monetization_types) ? partial.monetization_types.filter((item): item is string => typeof item === 'string') : draft.monetization_types,
    routes: typeof partial.routes === 'string' ? partial.routes : draft.routes,
    viral_topic: typeof partial.viral_topic === 'string' ? partial.viral_topic : draft.viral_topic,
    frequency: typeof partial.frequency === 'string' ? partial.frequency : draft.frequency,
  };
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function asText(value: unknown, fallback = '暂无数据'): string {
  return typeof value === 'string' && value.trim() ? value : fallback;
}

function asStringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is string => typeof item === 'string' && item.trim().length > 0);
}

function sourceLabel(asset: Asset): string {
  const source = asset.sources[0] ?? {};
  return asText(source.source_title ?? source.publisher ?? source.url ?? asset.source_type, asset.source_type);
}

function scriptFromOutput(output: Output): GeneratedScript {
  let content: Record<string, unknown> = asRecord(output.content_json);
  if (typeof output.content_json === 'string') {
    try { content = asRecord(JSON.parse(output.content_json)); } catch { content = {}; }
  }
  const sources = asStringList(content.sources ?? content.source_refs ?? content.references);
  return {
    title: asText(content.title, output.title),
    hook: asText(content.hook ?? content.opening ?? content.intro),
    body: asText(content.body ?? content.main_text ?? content.script),
    ending: asText(content.ending ?? content.call_to_action ?? content.cta),
    tags: asStringList(content.tags ?? content.hashtags),
    sources: sources.length > 0 ? sources : output.assets.map((item) => `资产 #${item.asset_id} · ${item.usage_type}`),
    outputId: output.id,
  };
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : '请求失败，请检查后端服务与 DeepSeek 配置。';
}

function scriptGenerationErrorMessage(error: unknown): string {
  if (error instanceof AgentApiError) {
    const reasons: Record<string, string> = {
      AGENT_REQUEST_FAILED: '模型连接或服务请求失败，可能是网络、代理、DNS 或远程服务异常，请检查后端到 DeepSeek 的连接后重试。',
      AGENT_TIMEOUT: '模型响应超时，请稍后重试。',
      NETWORK_ERROR: '前端无法连接后端服务，请检查后端是否正常运行。',
      OUTPUT_RUNNING_TIMEOUT: '模型任务仍在后台生成，等待时间较长，请稍后刷新查看。',
      OUTPUT_INVALID_JSON: '模型返回的脚本格式不完整，自动修复和重试均未成功。',
      OUTPUT_EVIDENCE_INVALID: '脚本引用了当前三库之外的资产，自动重试仍未通过证据校验。',
    };
    const reason = reasons[error.code];
    if (reason) return `脚本生成失败：${reason}（错误码：${error.code}）`;
  }
  return `脚本生成失败：${errorMessage(error)}`;
}

function isRetryableProfileError(error: unknown): boolean {
  return error instanceof AgentApiError && (error.code === 'PROFILE_AGENT_REQUEST_FAILED' || error.code === 'NETWORK_ERROR');
}

const PROFILE_AUTO_RETRY_LIMIT = 2;
const PROFILE_AUTO_RETRY_DELAYS = [500, 1500];

function waitForProfileRetry(attempt: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, PROFILE_AUTO_RETRY_DELAYS[attempt] ?? 1500));
}

const SCRIPT_AUTO_RETRY_LIMIT = 2;
const SCRIPT_AUTO_RETRY_DELAYS = [800, 1800];
const SCRIPT_STATUS_POLL_LIMIT = 180;
const SCRIPT_STATUS_POLL_INTERVAL = 2000;

function waitForScriptRetry(attempt: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, SCRIPT_AUTO_RETRY_DELAYS[attempt] ?? 1800));
}

function waitForScriptStatus(): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, SCRIPT_STATUS_POLL_INTERVAL));
}

function isRetryableScriptError(error: unknown): boolean {
  return error instanceof AgentApiError && [
    'AGENT_REQUEST_FAILED',
    'AGENT_TIMEOUT',
    'OUTPUT_EVIDENCE_INVALID',
    'OUTPUT_INVALID_JSON',
  ].includes(error.code);
}

async function waitForScriptOutput(bloggerId: number, outputId: number): Promise<Output> {
  let output = await requestJson<Output>(`/bloggers/${bloggerId}/outputs/${outputId}`);
  for (let attempt = 0; attempt < SCRIPT_STATUS_POLL_LIMIT; attempt += 1) {
    if (output.status !== 'pending' && output.status !== 'running') return output;
    await waitForScriptStatus();
    output = await requestJson<Output>(`/bloggers/${bloggerId}/outputs/${outputId}`);
  }
  throw new AgentApiError(408, 'OUTPUT_RUNNING_TIMEOUT', '脚本任务仍在生成中，请稍后重新查看。');
}

function libraryLabel(type: LibraryType): string {
  return type === 'knowledge' ? '知识库' : type === 'material' ? '素材库' : '算法库';
}

function ProfileEditorCard({ draft, mode, saving, onChange, onSubmit, onCancel }: { draft: ProfileDraft; mode: ProfileEditorMode; saving: boolean; onChange: (key: keyof ProfileDraft, value: string | string[]) => void; onSubmit: (event: FormEvent) => void; onCancel: () => void }) {
  const listValue = (key: 'content_types' | 'monetization_types') => draft[key].join('、');
  const updateList = (key: 'content_types' | 'monetization_types', value: string) => onChange(key, value.split(/[、,，]/).map((item) => item.trim()).filter(Boolean));
  return <form className="profile-card" onSubmit={onSubmit}>
    <h4><span className="idx">{mode === 'create' ? 'NEW' : 'EDIT'}</span>{mode === 'create' ? '新增博主画像' : '编辑当前博主画像'}</h4>
    <p className="profile-card-hint">{mode === 'create' ? '直接填写后保存，接着就能用真实 SQLite 建立三库。' : '修改后会写回当前博主；已有三库资产会保留，体检结果需要重新生成。'}</p>
    <div className="profile-form-grid">
      <label>博主名称<input value={draft.name} onChange={(event) => onChange('name', event.target.value)} required /></label>
      <label>主平台<input value={draft.platform} onChange={(event) => onChange('platform', event.target.value)} required placeholder="例如：抖音、小红书" /></label>
      <label>内容方向<input value={listValue('content_types')} onChange={(event) => updateList('content_types', event.target.value)} required placeholder="用顿号分隔" /></label>
      <label>表达风格<input value={draft.style} onChange={(event) => onChange('style', event.target.value)} required /></label>
      <label>粉丝阶段<input value={draft.follower_band} onChange={(event) => onChange('follower_band', event.target.value)} required /></label>
      <label>变现方向<input value={listValue('monetization_types')} onChange={(event) => updateList('monetization_types', event.target.value)} required placeholder="用顿号分隔" /></label>
      <label>常跑路线<input value={draft.routes} onChange={(event) => onChange('routes', event.target.value)} /></label>
      <label>最近爆款<input value={draft.viral_topic} onChange={(event) => onChange('viral_topic', event.target.value)} /></label>
      <label>更新频率<input value={draft.frequency} onChange={(event) => onChange('frequency', event.target.value)} /></label>
    </div>
    <div className="profile-card-actions"><span>{mode === 'create' ? '必填项完成后即可保存' : '保存后仍可继续走三库 → 体检 → 脚本'}</span><button type="button" className="pf-skip" onClick={onCancel}>取消</button><button className="cbtn" type="submit" disabled={saving}>{saving ? '保存中…' : mode === 'create' ? '保存新增博主' : '保存修改'}</button></div>
  </form>;
}

function ScriptCard({ script }: { script: GeneratedScript }) {
  return <article className="script-card"><div className="script-card-title"><span>OUTPUT #{script.outputId}</span><h3>{script.title}</h3></div><section><b>开头</b><p>{script.hook}</p></section><section><b>正文</b><p>{script.body}</p></section><section><b>结尾</b><p>{script.ending}</p></section>{script.tags.length > 0 && <div className="tag-row">{script.tags.map((tag) => <span key={tag}>#{tag}</span>)}</div>}<div className="source-trace"><strong>引用溯源</strong>{script.sources.map((source) => <span key={source}>✓ {source}</span>)}</div></article>;
}

function LibraryDrawer({ assets }: { assets: Asset[] }) {
  const [tab, setTab] = useState<'all' | LibraryType>('all');
  const [query, setQuery] = useState('');
  const filtered = useMemo(() => assets.filter((asset) => (tab === 'all' || asset.lib_type === tab) && `${asset.title}${asset.content}${asset.tags.join('')}`.toLowerCase().includes(query.toLowerCase())), [assets, query, tab]);
  return <><div className="drawer-tabs">{([['all', '全部'], ['knowledge', '知识库'], ['material', '素材库'], ['algorithm', '算法库']] as const).map(([id, label]) => <button className={tab === id ? 'on' : ''} onClick={() => setTab(id)} key={id}>{label}</button>)}</div><input className="drawer-search" value={query} onChange={(event) => setQuery(event.target.value)} placeholder="检索酸汤鱼、苗绣、拍法…" />{filtered.map((asset) => <article className="drawer-card" key={asset.id}><h4>{asset.title}<span className={`credibility c${asset.credibility}`}>可信 {asset.credibility}</span></h4><p>{asset.content}</p><small>{libraryLabel(asset.lib_type)} · {asset.category} · {sourceLabel(asset)}</small></article>)}{filtered.length === 0 && <p className="drawer-empty">暂无命中的真实资产。</p>}</>;
}

function HealthDrawer({ assessment, assets }: { assessment: Assessment | null; assets: Asset[] }) {
  if (!assessment) return <p className="drawer-empty">还没有体检结果，请先在聊天中执行“一键体检”。当前有 {assets.length} 条资产。</p>;
  const readiness = asRecord(assessment.feature_readiness);
  const suggestions = Array.isArray(assessment.suggestions) ? assessment.suggestions : Object.values(asRecord(assessment.suggestions));
  return <><div className="health-summary"><div className="ring"><strong>{Math.round(assessment.overall_score ?? 0)}</strong><span>/ 100</span></div><div><b>综合健康度</b><p>{asText(assessment.summary)}</p></div></div><div className="drawer-section-title">Agent 自创指标</div>{assessment.indicators.map((indicator) => <article className="indicator-card" key={indicator.id}><div><b>{indicator.name}</b><span>{Math.round(indicator.score)} 分 · 权重 {Math.round(indicator.weight * 100)}%</span></div><i><em style={{ width: `${Math.max(0, Math.min(100, indicator.score))}%` }} /></i><p>{indicator.reason || indicator.business_meaning || indicator.meaning}</p></article>)}<div className="drawer-section-title">功能就绪度</div><div className="readiness-line"><b>脚本生成</b><span>{asText(readiness.script ?? readiness.script_generation ?? readiness.content, '体检响应未提供说明')}</span></div>{suggestions.slice(0, 4).map((suggestion, index) => <div className="suggestion-line" key={`${index}-${String(suggestion)}`}>{String(index + 1).padStart(2, '0')} · {typeof suggestion === 'string' ? suggestion : asText(asRecord(suggestion).title ?? asRecord(suggestion).suggestion ?? asRecord(suggestion).content, 'Agent 建议')}</div>)}</>;
}

function ReportDrawer({ blogger, assets, assessment, script }: { blogger: Blogger | null; assets: Asset[]; assessment: Assessment | null; script: GeneratedScript | null }) {
  return <><div className="report-callout"><b>{blogger?.name ?? '当前博主'}</b><p>当前版本将真实链路集中在画像、三库、体检和脚本。原型中的发布、反馈和经营报告暂不使用模拟数据。</p></div><div className="report-stats"><div><b>{assets.length}</b><span>三库资产</span></div><div><b>{assessment ? Math.round(assessment.overall_score ?? 0) : '—'}</b><span>最近体检</span></div><div><b>{script ? '✓' : '—'}</b><span>脚本输出</span></div></div><p className="drawer-empty">后续接入发布与反馈接口后，这里再扩展为经营报告。</p></>;
}

export default function Home() {
  const [bloggerId, setBloggerId] = useState(DEFAULT_BLOGGER_ID);
  const [blogger, setBlogger] = useState<Blogger | null>(null);
  const [bloggers, setBloggers] = useState<Blogger[]>([]);
  const [assets, setAssets] = useState<Asset[]>([]);
  const [assessment, setAssessment] = useState<Assessment | null>(null);
  const [script, setScript] = useState<GeneratedScript | null>(null);
  const [draft, setDraft] = useState<ProfileDraft>(emptyProfileDraft());
  const [profileEditor, setProfileEditor] = useState<ProfileEditorMode | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [entered, setEntered] = useState(false);
  const [chatInput, setChatInput] = useState('');
  const [sessionId, setSessionId] = useState<number | null>(null);
  const [sessionStatus, setSessionStatus] = useState<SessionStatus>('idle');
  const [chatLoading, setChatLoading] = useState(false);
  const [busy, setBusy] = useState<Busy>('load');
  const [error, setError] = useState('');
  const [chatError, setChatError] = useState('');
  const [failedProfileTurn, setFailedProfileTurn] = useState<FailedProfileTurn | null>(null);
  const [currentOutput, setCurrentOutput] = useState<Output | null>(null);
  const [materialAwaitingContent, setMaterialAwaitingContent] = useState(false);
  const [materialDraft, setMaterialDraft] = useState<MaterialDraft | null>(null);
  const [conversations, setConversations] = useState<ConversationRecord[]>([]);
  const [folders, setFolders] = useState<ConversationFolder[]>([]);
  const [collapsedFolderIds, setCollapsedFolderIds] = useState<string[]>([]);
  const [currentConversationId, setCurrentConversationId] = useState('');
  const [activeFolderId, setActiveFolderId] = useState(`blogger-${DEFAULT_BLOGGER_ID}`);
  const [folderEditorOpen, setFolderEditorOpen] = useState(false);
  const [folderNameDraft, setFolderNameDraft] = useState('');
  const [folderBloggerIdDraft, setFolderBloggerIdDraft] = useState(DEFAULT_BLOGGER_ID);
  const [conversationsHydrated, setConversationsHydrated] = useState(false);
  const [toast, setToast] = useState('');
  const [drawer, setDrawer] = useState<Drawer>(null);
  const nextMessageId = useRef(0);
  const nextScriptVariant = useRef(0);
  const messagesEndRef = useRef<HTMLDivElement>(null);

  const notify = (message: string) => { setToast(message); window.setTimeout(() => setToast(''), 2600); };
  const pushMessage = (role: ChatMessage['role'], content: string, actions: ChatAction[] = [], messageScript?: GeneratedScript) => setMessages((current) => [...current, { id: nextMessageId.current++, role, content, actions, script: messageScript }]);
  const pushAssistant = (content: string, actions: ChatAction[] = [], messageScript?: GeneratedScript) => pushMessage('assistant', content, actions, messageScript);
  const pushUser = (content: string) => pushMessage('user', content);

  useEffect(() => {
    const stored = restoreConversations().sort((left, right) => right.updatedAt.localeCompare(left.updatedAt));
    const storedFolders = restoreConversationFolders();
    const folderMap = new Map(storedFolders.map((folder) => [folder.id, folder]));
    stored.forEach((conversation) => {
      if (!folderMap.has(conversation.folderId)) {
        folderMap.set(conversation.folderId, createConversationFolder(conversation.bloggerId, conversation.bloggerName, conversation.bloggerName, conversation.folderId));
      }
    });
    const initial = stored[0] ?? createConversation(DEFAULT_BLOGGER_ID, '', `blogger-${DEFAULT_BLOGGER_ID}`);
    if (!folderMap.has(initial.folderId)) {
      folderMap.set(initial.folderId, createConversationFolder(initial.bloggerId, initial.bloggerName, initial.bloggerName, initial.folderId));
    }
    const timer = window.setTimeout(() => {
      setConversations(stored.length > 0 ? stored : [initial]);
      setFolders([...folderMap.values()]);
      setCurrentConversationId(initial.id);
      setActiveFolderId(initial.folderId);
      setMessages(initial.messages);
      setSessionId(initial.sessionId);
      setSessionStatus(initial.sessionStatus);
      setDraft(initial.draft);
      setProfileEditor(initial.profileEditor);
      setFailedProfileTurn(initial.failedProfileTurn);
      setScript(initial.script);
      setCurrentOutput(initial.currentOutput);
      setMaterialAwaitingContent(initial.materialAwaitingContent);
      setMaterialDraft(initial.materialDraft);
      setConversationsHydrated(true);
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  useEffect(() => {
    if (!conversationsHydrated) return;
    window.localStorage.setItem(CONVERSATION_STORAGE_KEY, JSON.stringify(conversations));
  }, [conversations, conversationsHydrated]);

  useEffect(() => {
    if (!conversationsHydrated) return;
    window.localStorage.setItem(CONVERSATION_FOLDERS_STORAGE_KEY, JSON.stringify(folders));
  }, [folders, conversationsHydrated]);

  useEffect(() => {
    if (!conversationsHydrated || !currentConversationId) return;
    const timer = window.setTimeout(() => setConversations((current) => current.map((conversation) => conversation.id === currentConversationId ? {
      ...conversation,
      messages,
      sessionId,
      sessionStatus,
      draft,
      profileEditor,
      failedProfileTurn,
      script,
      currentOutput,
      materialAwaitingContent,
      materialDraft,
      updatedAt: new Date().toISOString(),
    } : conversation)), 0);
    return () => window.clearTimeout(timer);
  }, [blogger, conversationsHydrated, currentConversationId, currentOutput, draft, failedProfileTurn, materialAwaitingContent, materialDraft, messages, profileEditor, script, sessionId, sessionStatus]);

  const switchConversation = async (conversation: ConversationRecord) => {
    if (conversation.id === currentConversationId) return;
    setCurrentConversationId(conversation.id);
    setActiveFolderId(conversation.folderId);
    setMessages(conversation.messages);
    setSessionId(conversation.sessionId);
    setSessionStatus(conversation.sessionStatus);
    setDraft(conversation.draft);
    setProfileEditor(conversation.profileEditor);
    setFailedProfileTurn(conversation.failedProfileTurn);
    setScript(conversation.script);
    setCurrentOutput(conversation.currentOutput);
    setMaterialAwaitingContent(conversation.materialAwaitingContent);
    setMaterialDraft(conversation.materialDraft);
    setChatError('');
    setError('');
    setEntered(true);
    if (conversation.bloggerId !== bloggerId) {
      setBlogger(null);
      await loadData(conversation.bloggerId);
      setDraft(conversation.draft);
    }
  };

  const startNewConversation = () => {
    const fallbackFolder = createConversationFolder(bloggerId, blogger?.name ?? `#${bloggerId}`, blogger?.name ?? `#${bloggerId}`, `blogger-${bloggerId}`);
    const folder = folders.find((item) => item.id === activeFolderId) ?? fallbackFolder;
    if (!folders.some((item) => item.id === folder.id)) setFolders((current) => [...current, folder]);
    const conversation = createConversation(folder.bloggerId, folder.bloggerName, folder.id);
    setConversations((current) => [...current, conversation]);
    setCurrentConversationId(conversation.id);
    setActiveFolderId(folder.id);
    setMessages([]);
    setSessionId(null);
    setSessionStatus('idle');
    setDraft(emptyProfileDraft());
    setProfileEditor(null);
    setFailedProfileTurn(null);
    setScript(null);
    setCurrentOutput(null);
    setMaterialAwaitingContent(false);
    setMaterialDraft(null);
    setChatError('');
    setError('');
    setEntered(true);
    if (folder.bloggerId !== bloggerId) {
      setBlogger(null);
      void loadData(folder.bloggerId);
    }
    pushAssistant('新的对话开始了。你可以直接告诉我想做什么，或者先完成一轮画像采集。', [{ label: '开始 AI 采集', kind: 'start-profile' }]);
  };

  const deleteConversation = (conversation: ConversationRecord) => {
    if (!window.confirm(`确定删除会话“${conversationLabel(conversation)}”吗？删除后无法恢复。`)) return;
    const remaining = conversations.filter((item) => item.id !== conversation.id);
    if (conversation.id !== currentConversationId) {
      setConversations(remaining);
      notify('会话已删除');
      return;
    }
    const fallback = remaining
      .filter((item) => item.folderId === conversation.folderId)
      .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt))[0] ?? remaining[0];
    if (fallback) {
      setConversations(remaining);
      void switchConversation(fallback);
      notify('会话已删除，已切换到其他会话');
      return;
    }
    const replacement = createConversation(conversation.bloggerId, conversation.bloggerName, conversation.folderId);
    setConversations([...remaining, replacement]);
    setCurrentConversationId(replacement.id);
    setActiveFolderId(replacement.folderId);
    setMessages([]);
    setSessionId(null);
    setSessionStatus('idle');
    setDraft(emptyProfileDraft());
    setProfileEditor(null);
    setFailedProfileTurn(null);
    setScript(null);
    setCurrentOutput(null);
    setMaterialAwaitingContent(false);
    setMaterialDraft(null);
    setChatError('');
    setError('');
    setEntered(true);
    notify('会话已删除，已为当前文件夹准备新会话');
  };

  const loadData = useCallback(async (targetBloggerId = bloggerId) => {
    setBusy('load');
    setError('');
    try {
      const [profile, assetRows, assessmentRows] = await Promise.all([
        requestJson<Blogger>(`/bloggers/${targetBloggerId}`),
        requestJson<Asset[]>(`/bloggers/${targetBloggerId}/assets?page=1&page_size=100`),
        requestJson<Assessment[]>(`/bloggers/${targetBloggerId}/assessments?page=1&page_size=20`),
      ]);
      setBloggerId(targetBloggerId);
      setBlogger(profile);
      setDraft(draftFromBlogger(profile));
      setFolders((current) => current.map((folder) => folder.bloggerId === targetBloggerId ? {
        ...folder,
        bloggerName: profile.name,
        name: folder.id === `blogger-${targetBloggerId}` && folder.name.startsWith('#') ? profile.name : folder.name,
      } : folder));
      setAssets(assetRows);
      const latest = assessmentRows.find((item) => item.status === 'succeeded') ?? assessmentRows[0];
      if (latest) setAssessment(await requestJson<Assessment>(`/bloggers/${targetBloggerId}/assessments/${latest.id}`));
      else setAssessment(null);
    } catch (loadError) {
      setError(errorMessage(loadError));
    } finally {
      setBusy(null);
    }
  }, [bloggerId]);

  useEffect(() => {
    const timer = window.setTimeout(() => { void loadData(); }, 0);
    return () => window.clearTimeout(timer);
  }, [loadData]);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void listBloggers().then(setBloggers).catch(() => undefined);
    }, 0);
    return () => window.clearTimeout(timer);
  }, []);

  const bloggerOptions = bloggers.length > 0 ? bloggers : blogger ? [blogger] : [];

  const openFolderCreator = () => {
    setFolderNameDraft('');
    setFolderBloggerIdDraft(blogger?.id ?? bloggers[0]?.id ?? DEFAULT_BLOGGER_ID);
    setFolderEditorOpen(true);
    setChatError('');
  };

  const createFolder = (event: FormEvent) => {
    event.preventDefault();
    const selectedBlogger = bloggerOptions.find((item) => item.id === folderBloggerIdDraft);
    const folderName = folderNameDraft.trim();
    if (!folderName) {
      setChatError('请先填写文件夹名称。');
      return;
    }
    if (!selectedBlogger) {
      setChatError('暂时没有可选择的博主，请先新增博主信息。');
      return;
    }
    const folder = createConversationFolder(selectedBlogger.id, selectedBlogger.name, folderName);
    const conversation = createConversation(selectedBlogger.id, selectedBlogger.name, folder.id);
    setFolders((current) => [...current, folder]);
    setConversations((current) => [...current, conversation]);
    setCurrentConversationId(conversation.id);
    setActiveFolderId(folder.id);
    setMessages([]);
    setSessionId(null);
    setSessionStatus('idle');
    setDraft(emptyProfileDraft());
    setProfileEditor(null);
    setFailedProfileTurn(null);
    setScript(null);
    setCurrentOutput(null);
    setMaterialAwaitingContent(false);
    setMaterialDraft(null);
    setChatError('');
    setFolderEditorOpen(false);
    setEntered(true);
    if (selectedBlogger.id !== bloggerId) {
      setBlogger(null);
      void loadData(selectedBlogger.id);
    }
    pushAssistant(`已创建文件夹“${folder.name}”，关联博主“${selectedBlogger.name}”。现在可以在这个文件夹中开始新的会话。`, [{ label: '开始 AI 采集', kind: 'start-profile' }]);
    notify(`已创建博主文件夹：${folder.name}`);
  };

  const toggleFolder = (folder: ConversationFolder) => {
    setActiveFolderId(folder.id);
    setCollapsedFolderIds((current) => current.includes(folder.id) ? current.filter((id) => id !== folder.id) : [...current, folder.id]);
  };

  useEffect(() => {
    if (entered) messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [entered, messages, profileEditor, busy]);

  const openEditProfile = () => {
    if (!blogger) return;
    setSessionId(null);
    setSessionStatus('idle');
    setDraft(draftFromBlogger(blogger));
    setProfileEditor('edit');
    setEntered(true);
    pushAssistant('当前画像已经加载到编辑卡片里。修改后保存即可写回当前博主。');
  };

  const openCreateProfile = () => {
    setSessionId(null);
    setSessionStatus('idle');
    setDraft(emptyProfileDraft());
    setProfileEditor('create');
    setEntered(true);
    pushAssistant('可以。你可以直接填写这张卡片新增博主，也可以点“开始 AI 采集”，让我用多轮对话逐项认识你。');
  };

  const requestGuidedTurn = async (activeSessionId: number, value: string) => {
    const result = await requestJson<ProfileSessionReply>(`/profile-sessions/${activeSessionId}/messages`, {
      method: 'POST',
      body: { message: value, request_id: createIdempotencyKey('profile-guided-chat', { sessionId: activeSessionId, value }) },
    });
    setFailedProfileTurn(null);
    setChatError('');
    setDraft((current) => mergeProfile(current, result.collected_profile ?? {}));
    const nextStatus = result.status === 'confirming' ? 'confirming' : 'collecting';
    setSessionStatus(nextStatus);
    if (result.question) pushAssistant(result.question);
    else pushAssistant('画像信息已经采集完整，请核对右侧结构化画像，确认后创建博主。', [{ label: '确认画像并创建博主', kind: 'confirm-profile' }]);
  };

  const requestGuidedTurnWithRetry = async (activeSessionId: number, value: string) => {
    let lastError: unknown = null;
    for (let attempt = 0; attempt <= PROFILE_AUTO_RETRY_LIMIT; attempt += 1) {
      try {
        return await requestGuidedTurn(activeSessionId, value);
      } catch (requestError) {
        lastError = requestError;
        if (!isRetryableProfileError(requestError) || attempt === PROFILE_AUTO_RETRY_LIMIT) throw requestError;
        setChatError(`DeepSeek 请求暂时失败，正在自动重试（${attempt + 2}/${PROFILE_AUTO_RETRY_LIMIT + 1}）…`);
        await waitForProfileRetry(attempt);
      }
    }
    throw lastError;
  };

  const showProfileFailure = (failure: unknown, activeSessionId: number | null, value: string | null) => {
    const retryable = isRetryableProfileError(failure) && activeSessionId !== null && value !== null;
    setChatError(retryable ? 'DeepSeek 暂时不可用，当前画像进度已保留，可以重试本轮。' : errorMessage(failure));
    if (retryable) {
      setFailedProfileTurn({ sessionId: activeSessionId, value });
      pushAssistant('这轮画像采集没有完成，但已采集内容没有丢失。点击“重试本轮”即可继续，不会新增一条用户回答。', [{ label: '重试本轮', kind: 'retry-profile' }]);
    } else {
      setFailedProfileTurn(null);
      pushAssistant(`这轮画像采集没有完成：${errorMessage(failure)}`);
    }
  };

  const startProfileSession = async (initialMessage?: string) => {
    if (chatLoading || sessionId !== null) return;
    setProfileEditor(null);
    setDraft(emptyProfileDraft());
    setSessionStatus('collecting');
    setChatLoading(true);
    setChatError('');
    let createdSessionId: number | null = null;
    try {
      const result = await requestJson<ProfileSessionReply>('/profile-sessions', { method: 'POST' });
      createdSessionId = result.session_id;
      setSessionId(result.session_id);
      setSessionStatus(result.status === 'confirming' ? 'confirming' : 'collecting');
      pushAssistant(result.question ?? '我们开始认识你的创作方向吧。');
      if (initialMessage) await requestGuidedTurnWithRetry(result.session_id, initialMessage);
    } catch (sessionError) {
      showProfileFailure(sessionError, initialMessage ? createdSessionId : null, initialMessage ?? null);
    } finally {
      setChatLoading(false);
    }
  };

  const buildLibrary = async () => {
    if (!blogger) return;
    if (assets.length > 0) {
      setDrawer('library');
      pushAssistant(`当前三库已有 ${assets.length} 条真实资产，我先打开资产面板供你检索。`);
      return;
    }
    setBusy('build');
    setError('');
    try {
      const result = await requestJson<BuildRun>(`/bloggers/${bloggerId}/build-runs`, { method: 'POST', body: { idempotency_key: createIdempotencyKey('build', { bloggerId }) } });
      if (result.status !== 'succeeded') throw new Error(result.error_message ?? `建库未成功：${result.status}`);
      const nextAssets = await requestJson<Asset[]>(`/bloggers/${bloggerId}/assets?page=1&page_size=100`);
      setAssets(nextAssets);
      pushAssistant(`库建好了：知识库 ${nextAssets.filter((asset) => asset.lib_type === 'knowledge').length} 条 · 素材库 ${nextAssets.filter((asset) => asset.lib_type === 'material').length} 条 · 算法库 ${nextAssets.filter((asset) => asset.lib_type === 'algorithm').length} 条。下一步可以做一次体检。`, [{ label: '查看我的库', kind: 'open-library' }, { label: '一键体检', kind: 'assess' }]);
      notify('三库已按当前画像建立');
    } catch (buildError) {
      setError(errorMessage(buildError));
      pushAssistant(`建库失败：${errorMessage(buildError)}`);
    } finally {
      setBusy(null);
    }
  };

  const assessAssets = async () => {
    if (assets.length === 0) {
      pushAssistant('还没有可体检的资产，先把三库建起来。', [{ label: '一键建库', kind: 'build' }]);
      return;
    }
    setBusy('assess');
    setError('');
    try {
      const result = await requestJson<Assessment>(`/bloggers/${bloggerId}/assessments`, { method: 'POST', body: { idempotency_key: createIdempotencyKey('assessment', { bloggerId }), prompt_version: 'phase2-v1' } });
      setAssessment(result);
      if (result.status !== 'succeeded') throw new Error(result.error_message ?? `体检未成功：${result.status}`);
      pushAssistant(`体检完成，综合健康度 ${Math.round(result.overall_score ?? 0)} 分。指标、权重和建议已经写入真实体检结果。`, [{ label: '查看体检', kind: 'open-health' }, { label: '生成脚本', kind: 'generate' }]);
      notify(`体检完成，综合分 ${Math.round(result.overall_score ?? 0)}`);
    } catch (assessError) {
      setError(errorMessage(assessError));
      pushAssistant(`体检失败：${errorMessage(assessError)}`);
    } finally {
      setBusy(null);
    }
  };

  const generateScript = async (topic = blogger?.viral_topic ?? '贵州酸汤鱼与乡村生活', forceNew = false) => {
    if (!assessment || assessment.status !== 'succeeded') {
      pushAssistant('生成脚本前需要先完成一次成功体检。', [{ label: assets.length ? '一键体检' : '先建三库', kind: assets.length ? 'assess' : 'build' }]);
      return;
    }
    setBusy('generate');
    setError('');
    try {
      const selectedPlatform = blogger?.platform?.split('、')[0] ?? '抖音';
      const requestFingerprint = forceNew
        ? { bloggerId, assessmentId: assessment.id, topic, variant: `${crypto.randomUUID()}-${nextScriptVariant.current++}` }
        : { bloggerId, assessmentId: assessment.id, topic };
      const idempotencyKey = createIdempotencyKey('script', requestFingerprint);
      const requestBody = { assessment_id: assessment.id, idempotency_key: idempotencyKey, topic, user_instruction: `${topic}。请将 platform 字段填写为单一平台“${selectedPlatform}”，不要返回多平台字符串。`, platform: selectedPlatform, content_type: blogger?.content_types?.[0] ?? '口播视频' };
      let output: Output | null = null;
      for (let attempt = 0; attempt <= SCRIPT_AUTO_RETRY_LIMIT; attempt += 1) {
        try {
          if (attempt === 0) {
            output = await requestJson<Output>(`/bloggers/${bloggerId}/outputs/generate/script`, { method: 'POST', body: requestBody });
          } else {
            const failedOutputs = await listScriptOutputs(bloggerId, { status: 'failed', page: 1, page_size: 50 });
            const failedOutput = failedOutputs.find((item) => item.idempotency_key === idempotencyKey);
            if (!failedOutput) throw new AgentApiError(422, 'OUTPUT_RETRY_NOT_FOUND', '没有找到可重试的脚本任务。');
            output = await retryScriptOutput(bloggerId, failedOutput.id);
          }
          if (output.status === 'pending' || output.status === 'running') {
            output = await waitForScriptOutput(bloggerId, output.id);
          }
          if (output.status !== 'succeeded') {
            throw new AgentApiError(422, output.error_code ?? 'OUTPUT_GENERATION_FAILED', output.error_message ?? `脚本生成未成功：${output.status}`);
          }
          break;
        } catch (generationAttemptError) {
          if (!isRetryableScriptError(generationAttemptError) || attempt === SCRIPT_AUTO_RETRY_LIMIT) throw generationAttemptError;
          setError(`脚本生成暂时失败，正在自动重试（${attempt + 2}/${SCRIPT_AUTO_RETRY_LIMIT + 1}）…`);
          await waitForScriptRetry(attempt);
        }
      }
      if (!output) throw new Error('脚本生成未返回结果。');
      setError('');
      const nextScript = scriptFromOutput(output);
      setCurrentOutput(output);
      setScript(nextScript);
      pushAssistant('脚本已经生成，引用了体检结果和三库资产。', [{ label: '再生成一版', kind: 'generate', topic, forceNew: true }], nextScript);
      notify(`脚本已生成，输出 #${output.id}`);
    } catch (generateError) {
      const message = scriptGenerationErrorMessage(generateError);
      setError(message);
      pushAssistant(message, [{ label: '重新生成脚本', kind: 'generate', topic }]);
    } finally {
      setBusy(null);
    }
  };

  const prepareMaterialSave = (content: string) => {
    if (!blogger) {
      pushAssistant('请先选择或创建一个博主，再把内容保存到对应的素材库。');
      return;
    }
    const nextDraft = createMaterialDraft(content);
    setMaterialDraft(nextDraft);
    setMaterialAwaitingContent(false);
    pushAssistant(`我准备把这段内容保存到“${blogger.name}”的素材库：\n\n${nextDraft.content}\n\n分类：${nextDraft.category} · 标签：${nextDraft.tags.join('、')}`, [
      { label: '确认保存到素材库', kind: 'confirm-material' },
      { label: '取消保存', kind: 'cancel-material', ghost: true },
    ]);
  };

  const requestMaterialSave = (value: string) => {
    const content = extractMaterialContent(value);
    if (content) {
      prepareMaterialSave(content);
      return;
    }
    setMaterialAwaitingContent(true);
    pushAssistant('可以保存。请把要放入素材库的具体内容直接发给我；我会先生成标题、分类和标签预览，确认后才写入。');
  };

  const confirmMaterialSave = async () => {
    if (!materialDraft || !blogger) return;
    setBusy('save');
    setError('');
    try {
      const asset = await createAsset(bloggerId, materialDraft);
      setAssets((current) => [asset, ...current.filter((item) => item.id !== asset.id)]);
      setMaterialDraft(null);
      setMaterialAwaitingContent(false);
      pushAssistant(`已保存到“${blogger.name}”的素材库，资产 #${asset.id}。`, [{ label: '查看我的库', kind: 'open-library' }]);
      notify('素材已保存');
    } catch (saveError) {
      const message = errorMessage(saveError);
      setError(message);
      pushAssistant(`素材保存失败：${message}`);
    } finally {
      setBusy(null);
    }
  };

  const cancelMaterialSave = () => {
    setMaterialDraft(null);
    setMaterialAwaitingContent(false);
    pushAssistant('已取消保存，这段内容不会写入素材库。');
  };

  const reviseCurrentScript = async (instruction: string) => {
    if (!script || !currentOutput) {
      pushAssistant('当前还没有可修改的脚本。你可以先说“生成一条脚本”，完成后再修改开头、正文或结尾。', [{ label: '生成脚本', kind: 'generate' }]);
      return;
    }
    const edit = extractScriptEdit(instruction);
    if (!edit) {
      pushAssistant('我可以修改当前脚本。请明确要改哪一部分，例如“把开头改成：一根针，一根线，能藏住多少故事？”或“把结尾改成：评论区告诉我”。');
      return;
    }
    let currentContent: Record<string, unknown> = asRecord(currentOutput.content_json);
    if (typeof currentOutput.content_json === 'string') {
      try { currentContent = asRecord(JSON.parse(currentOutput.content_json)); } catch { currentContent = {}; }
    }
    const nextContent = { ...currentContent };
    nextContent[edit.field] = edit.field === 'tags'
      ? edit.nextValue.split(/[、,，]/).map((item) => item.trim()).filter(Boolean)
      : edit.nextValue;
    setBusy('save');
    setError('');
    try {
      const revised = await reviseScriptOutput(bloggerId, currentOutput.id, {
        content_json: nextContent,
        ...(edit.field === 'title' ? { title: edit.nextValue } : {}),
      });
      const nextScript = scriptFromOutput(revised);
      setCurrentOutput(revised);
      setScript(nextScript);
      pushAssistant(`已修改脚本${edit.field === 'title' ? '标题' : edit.field === 'hook' ? '开头' : edit.field === 'body' ? '正文' : edit.field === 'ending' ? '结尾' : '标签'}，并保存为新版本。`, [], nextScript);
      notify(`脚本已保存为新版本 #${revised.id}`);
    } catch (reviseError) {
      const message = errorMessage(reviseError);
      setError(message);
      pushAssistant(`脚本修改失败：${message}`);
    } finally {
      setBusy(null);
    }
  };

  const bindActiveConversationToBlogger = (nextBlogger: Blogger) => {
    const folderId = `blogger-${nextBlogger.id}`;
    setFolders((current) => current.some((folder) => folder.id === folderId)
      ? current.map((folder) => folder.id === folderId ? { ...folder, bloggerName: nextBlogger.name, name: folder.name.startsWith('#') ? nextBlogger.name : folder.name } : folder)
      : [...current, createConversationFolder(nextBlogger.id, nextBlogger.name, nextBlogger.name, folderId)]);
    setConversations((current) => current.map((conversation) => conversation.id === currentConversationId ? { ...conversation, folderId, bloggerId: nextBlogger.id, bloggerName: nextBlogger.name } : conversation));
    setActiveFolderId(folderId);
  };

  const saveProfile = async (event: FormEvent) => {
    event.preventDefault();
    if (profileEditor === null) return;
    setBusy('save');
    setError('');
    try {
      if (profileEditor === 'create') {
        const created = await createBlogger(draft);
        setBloggerId(created.id);
        setBlogger(created);
        setDraft(draftFromBlogger(created));
        setAssets([]);
        setAssessment(null);
        setScript(null);
        bindActiveConversationToBlogger(created);
        setProfileEditor(null);
        pushAssistant(`已新增博主“${created.name}”。接下来可以直接建立三库。`, [{ label: '一键建库', kind: 'build' }, { label: '开始 AI 采集', kind: 'start-profile' }]);
        notify(`已新增博主：${created.name}`);
      } else {
        const updated = await updateBlogger(bloggerId, draft);
        setBlogger(updated);
        setDraft(draftFromBlogger(updated));
        setConversations((current) => current.map((conversation) => conversation.id === currentConversationId ? { ...conversation, bloggerName: updated.name } : conversation));
        setFolders((current) => current.map((folder) => folder.bloggerId === updated.id ? { ...folder, bloggerName: updated.name } : folder));
        setAssessment(null);
        setScript(null);
        setProfileEditor(null);
        pushAssistant(`当前博主“${updated.name}”已保存。已有三库资产保留，建议重新做一次体检。`, [{ label: '查看我的库', kind: 'open-library' }, { label: '一键体检', kind: 'assess' }]);
        notify('画像修改已保存');
      }
    } catch (saveError) {
      setError(errorMessage(saveError));
      pushAssistant(`画像保存失败：${errorMessage(saveError)}`);
    } finally {
      setBusy(null);
    }
  };

  const confirmGuidedProfile = async () => {
    if (!sessionId) return;
    setBusy('save');
    setChatLoading(true);
    setError('');
    try {
      const created = await requestJson<Blogger>(`/profile-sessions/${sessionId}/confirm`, { method: 'POST', body: draft });
      setBloggerId(created.id);
      setBlogger(created);
      setDraft(draftFromBlogger(created));
      setAssets([]);
      setAssessment(null);
      setScript(null);
      bindActiveConversationToBlogger(created);
      setSessionId(null);
      setSessionStatus('idle');
      pushAssistant(`画像确认完成，已新增博主“${created.name}”。现在可以从三库开始。`, [{ label: '一键建库', kind: 'build' }]);
      notify(`画像已确认：${created.name}`);
    } catch (confirmError) {
      setError(errorMessage(confirmError));
      pushAssistant(`确认失败：${errorMessage(confirmError)}`);
    } finally {
      setBusy(null);
      setChatLoading(false);
    }
  };

  const retryProfileTurn = async () => {
    if (!failedProfileTurn || chatLoading || busy !== null) return;
    const { sessionId: activeSessionId, value } = failedProfileTurn;
    setChatLoading(true);
    setChatError('');
    try {
      await requestGuidedTurnWithRetry(activeSessionId, value);
    } catch (retryError) {
      showProfileFailure(retryError, activeSessionId, value);
    } finally {
      setChatLoading(false);
    }
  };

  const handleAction = (action: ChatAction) => {
    if (action.kind === 'start-profile') { void startProfileSession(); return; }
    if (action.kind === 'edit-profile') { openEditProfile(); return; }
    if (action.kind === 'create-profile') { openCreateProfile(); return; }
    if (action.kind === 'build') { void buildLibrary(); return; }
    if (action.kind === 'assess') { void assessAssets(); return; }
    if (action.kind === 'generate') { void generateScript(action.topic, action.forceNew); return; }
    if (action.kind === 'open-library') { setDrawer('library'); return; }
    if (action.kind === 'open-health') { setDrawer('health'); return; }
    if (action.kind === 'open-report') { setDrawer('report'); return; }
    if (action.kind === 'retry-profile') { void retryProfileTurn(); return; }
    if (action.kind === 'confirm-profile') { void confirmGuidedProfile(); }
    if (action.kind === 'confirm-material') { void confirmMaterialSave(); return; }
    if (action.kind === 'cancel-material') { cancelMaterialSave(); }
  };

  const requestModelChat = async (value: string, previousMessages: ChatMessage[]) => {
    setChatLoading(true);
    setChatError('');
    const conversation = previousMessages
      .filter((message) => message.content.trim())
      .slice(-12)
      .map((message) => ({ role: message.role, content: message.content }));
    try {
      const result = await chatWithAgent(bloggerId, {
        message: value,
        conversation,
        request_id: createIdempotencyKey('chat', { bloggerId, message: value, conversation }),
      });
      if (!result.reply?.trim()) throw new AgentApiError(502, 'CHAT_EMPTY_REPLY', '模型没有返回有效内容。');
      pushAssistant(result.reply.trim());
    } catch (chatRequestError) {
      const message = errorMessage(chatRequestError);
      setChatError(`模型回复失败：${message}`);
      pushAssistant(`模型暂时无法回复：${message}`);
    } finally {
      setChatLoading(false);
    }
  };

  const handleCommand = (value: string) => {
    const intent = getChatIntent(value);
    if (intent === 'profile') {
      void startProfileSession(/(?:我叫|我是|平台|粉丝|变现|更新频率|内容方向)/.test(value) ? value : undefined);
      return;
    }
    if (intent === 'material') { requestMaterialSave(value); return; }
    if (intent === 'edit-script') { void reviseCurrentScript(value); return; }
    if (intent === 'generate-script') { void generateScript(value); return; }
    if (intent === 'build') { void buildLibrary(); return; }
    if (intent === 'assess') { void assessAssets(); return; }
    void requestModelChat(value, messages);
  };

  const submitChat = async (event: FormEvent) => {
    event.preventDefault();
    const value = chatInput.trim();
    if (!value || chatLoading || busy !== null) return;
    setChatInput('');
    pushUser(value);
    if (materialAwaitingContent && sessionId === null) {
      prepareMaterialSave(value);
      return;
    }
    if (sessionId && sessionStatus === 'collecting') {
      setChatLoading(true);
      setChatError('');
      try { await requestGuidedTurnWithRetry(sessionId, value); } catch (chatRequestError) { showProfileFailure(chatRequestError, sessionId, value); } finally { setChatLoading(false); }
      return;
    }
    if (sessionStatus === 'confirming') { pushAssistant('画像已经完整，请先点击“确认画像并创建博主”。'); return; }
    handleCommand(value);
  };

  const enterWithPrompt = (value: string, onboard = false) => {
    setEntered(true);
    if (onboard) {
      pushAssistant('好，我们用自然语言一步步完成画像。我会根据你的回答继续追问，不需要选择字段。', [{ label: '开始 AI 采集', kind: 'start-profile' }]);
    } else {
      pushUser(value);
      handleCommand(value);
    }
  };

  const submitLanding = (event: FormEvent) => {
    event.preventDefault();
    const input = event.currentTarget.querySelector('textarea') as HTMLTextAreaElement | null;
    const value = input?.value.trim() ?? '';
    if (!value) return;
    setEntered(true);
    pushUser(value);
    handleCommand(value);
    if (input) input.value = '';
  };

  const conversationMeta = useMemo(() => {
    const turns = messages.filter((message) => message.role === 'user').length;
    const state = sessionStatus === 'collecting' ? '画像采集中' : sessionStatus === 'confirming' ? '待确认画像' : '内容助手';
    return `${blogger?.platform ?? '未选择平台'} · ${turns} 轮 · ${state}`;
  }, [blogger, messages, sessionStatus]);
  const conversationFolders = useMemo(() => {
    return folders.map((folder) => ({
      ...folder,
      conversations: conversations
        .filter((conversation) => conversation.folderId === folder.id)
        .sort((left, right) => right.updatedAt.localeCompare(left.updatedAt)),
    }));
  }, [conversations, folders]);

  return <main className="prototype-app">
    <div className="bgdeco" aria-hidden="true"><span /><span /><i>黔</i></div>
    <section className={`landing ${entered ? 'hide' : ''}`}>
      <span className="demo-flag">真实联调 · SQLite + DeepSeek</span>
      <div className="landing-inner"><div className="seal">黔</div><h1 className="hello">你好，我是<span>贵客松</span> · 文旅内容 AI 编辑</h1><p className="sub">把你在贵州的每一次拍摄、每一家小店、每一段民俗，变成<b>可检索、可复用、越用越懂你</b>的数据资产。<br />你可以直接说想做什么，也可以从画像采集开始。</p><form className="hero-input" onSubmit={submitLanding}><textarea rows={2} placeholder="例：我叫小黔，主要拍贵州美食和乡村生活…" /><div className="hero-foot"><span><kbd>Enter</kbd> 发送 · <kbd>Shift</kbd>+<kbd>Enter</kbd> 换行</span><button className="send-btn" type="submit">发送 →</button></div></form><div className="suggest"><button type="button" onClick={() => enterWithPrompt('', true)}><span>◆</span> 开始建档，30 秒画像</button><button type="button" onClick={() => enterWithPrompt('写一条酸汤鱼的开头')}>写一条酸汤鱼的开头</button><button type="button" onClick={() => enterWithPrompt('黔东南非遗怎么做')}>黔东南非遗怎么做</button><button type="button" onClick={() => enterWithPrompt('一键体检')}>一键体检</button></div></div>
    </section>
    <div className={`shell ${entered ? 'show' : ''}`}>
      <header className="topbar"><button className="brand" onClick={() => setEntered(false)}><span className="mini-seal">黔</span>贵客松 <small>文旅博主 AI 编辑</small></button><span className="demo-tag">真实数据</span><div className="topbar-mid">博主 <b>{blogger?.name ?? `#${bloggerId}`}</b></div><div className="topbar-spacer" /><button onClick={() => setDrawer('library')}>我的库</button><button onClick={() => setDrawer('health')}>体检</button><button onClick={() => setDrawer('report')}>报告</button></header>
      <div className="app"><aside className="sidebar"><button className="new-chat" onClick={startNewConversation}>＋ 新对话</button><button className="folder-create-trigger" onClick={openFolderCreator}>＋ 新建博主文件夹</button>{folderEditorOpen && <form className="folder-form" onSubmit={createFolder}><label>文件夹名称<input value={folderNameDraft} onChange={(event) => setFolderNameDraft(event.target.value)} placeholder="例如：丹寨苗绣专题" autoFocus /></label><label>关联博主<select value={folderBloggerIdDraft} onChange={(event) => setFolderBloggerIdDraft(Number(event.target.value))} disabled={bloggerOptions.length === 0}>{bloggerOptions.map((item) => <option value={item.id} key={item.id}>{item.name} · {item.platform}</option>)}</select></label><div className="folder-form-actions"><button type="button" onClick={() => setFolderEditorOpen(false)}>取消</button><button type="submit" disabled={bloggerOptions.length === 0}>创建</button></div></form>}<div className="sb-label">会话</div><div className="conv-list">{conversationFolders.map((folder) => { const collapsed = collapsedFolderIds.includes(folder.id); return <section className={`conv-folder ${folder.id === activeFolderId ? 'active' : ''}`} key={folder.id}><button type="button" className="conv-folder-title" onClick={() => toggleFolder(folder)} aria-expanded={!collapsed} title={collapsed ? `展开${folder.name}对话` : `收起${folder.name}对话`}>{collapsed ? '▸' : '▾'} {folder.name}<small> · {folder.bloggerName}</small></button>{!collapsed && folder.conversations.map((conversation) => <div className="conv-row" key={conversation.id}><button type="button" className={`conv-item ${conversation.id === currentConversationId ? 'current' : ''}`} onClick={() => void switchConversation(conversation)}><div className="conv-title">{conversationLabel(conversation)}</div><div className="conv-meta">{conversation.id === currentConversationId ? conversationMeta : `${conversation.messages.filter((message) => message.role === 'user').length} 轮 · ${conversation.sessionStatus === 'collecting' ? '画像采集中' : '内容助手'}`}</div></button><button type="button" className="conv-delete" onClick={(event) => { event.stopPropagation(); deleteConversation(conversation); }} aria-label={`删除会话 ${conversationLabel(conversation)}`} title="删除会话">×</button></div>)}</section>; })}</div><div className="quick"><div className="sb-label">快捷入口</div><button onClick={() => void startProfileSession()}>✦ 开始 AI 采集</button><button onClick={openEditProfile}>✎ 编辑当前画像</button><button onClick={() => setDrawer('library')}>📚 我的库 · {assets.length}</button><button onClick={() => setDrawer('health')}>◎ 体检{assessment ? ` · ${Math.round(assessment.overall_score ?? 0)}` : ''}</button><button onClick={openCreateProfile}>＋ 新增博主信息</button></div></aside><main className="chat"><div className="messages">{blogger && <div className="profile-chip"><span className="dot" /><span>画像</span><b>{blogger.name}</b><span>·</span><span>{blogger.platform}</span><span>·</span><span>{blogger.content_types.join('/')}</span><span>·</span><span>{blogger.style}</span><button onClick={openEditProfile}>编辑</button></div>}{messages.map((message) => <div className={`msg ${message.role}`} key={message.id}>{message.role === 'user' ? <div className="bubble">{message.content}</div> : <><div className="avatar">黔</div><div className="msg-body"><div className="who">AI 编辑</div><div className="bubble"><p>{message.content}</p>{message.script && <ScriptCard script={message.script} />}{message.actions && message.actions.length > 0 && <div className="message-actions">{message.actions.map((action) => <button key={`${message.id}-${action.kind}-${action.label}`} className={`cbtn ${action.ghost ? 'ghost' : ''}`} onClick={() => handleAction(action)} disabled={chatLoading || busy !== null}>{action.label}</button>)}</div>}</div></div></>}</div>)}{profileEditor && <div className="msg ai"><div className="avatar">黔</div><div className="msg-body"><div className="who">AI 编辑</div><ProfileEditorCard draft={draft} mode={profileEditor} saving={busy === 'save'} onChange={(key, value) => setDraft((current) => ({ ...current, [key]: value }))} onSubmit={saveProfile} onCancel={() => setProfileEditor(null)} /></div></div>}{error && <div className="inline-error">{error}</div>}{chatError && <div className="inline-error">{chatError}</div>}<div ref={messagesEndRef} /></div><form className="inputbar" onSubmit={submitChat}><div className="inputbar-inner"><textarea value={chatInput} onChange={(event) => setChatInput(event.target.value)} onKeyDown={(event) => { if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} rows={1} placeholder={sessionStatus === 'collecting' ? '继续回答 AI 的问题…' : '继续问 AI 编辑… 例：帮我想条酸汤鱼的开头'} disabled={chatLoading || busy !== null || sessionStatus === 'confirming'} /><div className="inputbar-foot"><span><kbd>Enter</kbd> 发送</span><button className="send-btn" type="submit" disabled={chatLoading || busy !== null || sessionStatus === 'confirming'}>{chatLoading ? '思考中…' : '发送'}</button></div></div></form></main></div>
    </div>
    {drawer && <div className="mask" onClick={() => setDrawer(null)}><aside className="drawer" onClick={(event) => event.stopPropagation()}><div className="dr-head"><h2>{drawer === 'library' ? '我的库 · 三库资产' : drawer === 'health' ? '体检报告 · Agent 指标' : '当前链路状态'}</h2><button className="dr-close" onClick={() => setDrawer(null)}>✕</button></div><div className="dr-body">{drawer === 'library' && <LibraryDrawer assets={assets} />}{drawer === 'health' && <HealthDrawer assessment={assessment} assets={assets} />}{drawer === 'report' && <ReportDrawer blogger={blogger} assets={assets} assessment={assessment} script={script} />}</div></aside></div>}
    {toast && <div className="toast">✓ {toast}</div>}
  </main>;
}
