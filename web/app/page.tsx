'use client';

import { FormEvent, useCallback, useEffect, useMemo, useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import QaWorkbench from './qa-workbench';

type Health = { status: string; node_id: string; codex_version: string | null; active_jobs: number; database: string };
type Repository = { id: string; name: string; root_path: string; default_branch: string; validation_commands: string[]; build_root: string | null; firmware_image_root: string | null; enabled: boolean };
type RepositoryBranches = { repository_id: string; default_branch: string; branches: string[] };
type RepositoryWorktree = { path: string; branch: string | null; commit: string; is_main: boolean };
type RepositoryWorktrees = { repository_id: string; worktrees: RepositoryWorktree[] };
type Stage = { id: string; kind: string; sequence: number; status: string; result: Record<string, unknown> | null };
type Approval = { id: string; stage_id: string; kind: string; status: string; summary: string; requested_at: string; comment: string | null };
type AgentSession = { id: string; node_id: string; role: string; codex_thread_id: string | null; cwd: string; active: boolean };
type FirmwareArtifact = { id: string; coding_run_id: string; partition: string; filename: string; local_path: string; sha256: string; size_bytes: number; source: string; verified_at: string; created_at: string };
type CodingRun = {
  id: string;
  display_number: number;
  repository_id: string;
  parent_run_id: string | null;
  source_qa_run_id: string | null;
  title: string;
  requirement: string;
  acceptance_criteria: string[];
  source_branch: string;
  work_branch: string;
  worktree_mode: 'new' | 'existing' | 'reuse_new_branch';
  worktree_branch: string;
  worktree_initial_branch: string | null;
  worktree_path: string | null;
  status: string;
  current_stage: string;
  repair_attempts: number;
  last_error: string | null;
  created_at: string;
  completed_at: string | null;
  closed_at: string | null;
  stages: Stage[];
  sessions: AgentSession[];
  approvals: Approval[];
  firmware_artifacts: FirmwareArtifact[];
};
type RunEvent = { id: string; event_type: string; message: string | null; created_at: string };
type MergeReadiness = { eligible: boolean; status: string; reason: string; qa_run_id: string | null; source_branch: string; target_branch: string; merge_result: Record<string, unknown> | null };
type FollowupDraft = {
  parentRunId: string;
  sourceQaRunId: string;
  title: string;
  requirement: string;
  acceptanceCriteria: string[];
  sourceBranch: string;
};

const statusLabels: Record<string, string> = {
  created: '已创建', preparing: '准备工作区', planning: '方案生成中', awaiting_plan_approval: '等待方案批准',
  implementing: '编码中', building: '固件构建中', validating: '验证中', repairing: '修复中', reviewing: '审查中',
  awaiting_completion_approval: '等待最终确认', completed: '已完成', failed: '失败', blocked: '已暂停',
  cancelling: '正在取消', cancelled: '已取消',
};

const pipeline = [
  ['prepare', '工作区'], ['plan', '方案'], ['implement', '编码'], ['validate', '验证'], ['review', '审查'], ['build', '构建'], ['complete', '完成'],
];

const recordOnlyDeletableStatuses = new Set([
  'created', 'awaiting_plan_approval', 'awaiting_completion_approval', 'completed', 'failed', 'blocked', 'cancelled',
]);

const stageKindLabels: Record<string, string> = {
  prepare: '工作区', plan: '设计方案', implement: '编码结果', build: '固件构建', validate: '验证结果', repair: '修复结果',
  review: '审查结果', discuss: 'Coding Agent 对话', merge: '主分支合并', complete: '完成记录',
};

const stageStatusLabels: Record<string, string> = {
  pending: '等待中', running: '进行中', awaiting_approval: '等待确认', succeeded: '已完成', skipped: '已被新版替换', failed: '失败', cancelled: '已取消',
};

function stringItems(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : [];
}

function markdownList(title: string, value: unknown, ordered = false): string | null {
  const items = stringItems(value);
  if (!items.length) return null;
  return `## ${title}\n\n${items.map((item, index) => `${ordered ? `${index + 1}.` : '-'} ${item}`).join('\n')}`;
}

function codeBlock(value: unknown): string {
  return `~~~text\n${typeof value === 'string' && value.length ? value : '（无输出）'}\n~~~`;
}

function stageResultMarkdown(stage: Stage): string {
  const result = stage.result;
  if (!result) return '该阶段尚未生成可展示的结果。';
  const sections: string[] = [];
  if (typeof result.summary === 'string' && result.summary.trim()) sections.push(`# 结果摘要\n\n${result.summary}`);
  if (result.operator_recovery && typeof result.operator_recovery === 'object') {
    const recovery = result.operator_recovery as Record<string, unknown>;
    sections.push([
      '## 人工恢复输入',
      String(recovery.message ?? '未记录'),
      `\n- 提交人：${String(recovery.submitted_by ?? '未记录')}`,
      `- 恢复前错误：${String(recovery.previous_error ?? '未记录')}`,
    ].join('\n'));
  }

  const knownLists: Array<[string, unknown, boolean?]> = [
    ['实施步骤', result.plan, true],
    ['风险与注意事项', result.risks],
    ['验收检查', result.acceptance_check],
    ['变更文件', result.changed_files],
    ['遗留风险', result.remaining_risks],
    ['审查发现', result.findings],
    ['最终检查', result.final_checks],
    ['阻塞项', result.blockers],
  ];
  knownLists.forEach(([title, value, ordered]) => {
    const section = markdownList(title, value, ordered);
    if (section) sections.push(section);
  });

  if (Array.isArray(result.tests) && result.tests.length) {
    const rows = result.tests.flatMap((item) => {
      if (!item || typeof item !== 'object') return [];
      const test = item as Record<string, unknown>;
      return [`### ${typeof test.command === 'string' ? `\`${test.command}\`` : '测试'}\n\n${typeof test.result === 'string' ? test.result : '未记录结果'}`];
    });
    if (rows.length) sections.push(`## Agent 报告的测试\n\n${rows.join('\n\n')}`);
  }

  if (Array.isArray(result.checks) && result.checks.length) {
    const checks = result.checks.flatMap((item, index) => {
      if (!item || typeof item !== 'object') return [];
      const check = item as Record<string, unknown>;
      const passed = check.passed === true;
      const meta = [`退出码：${String(check.exit_code ?? '未知')}`];
      if (check.timed_out === true) meta.push('已超时');
      return [
        `### ${passed ? '✓' : '✕'} ${typeof check.command === 'string' ? `\`${check.command}\`` : `验证命令 ${index + 1}`}`,
        meta.join(' · '),
        `**标准输出**\n\n${codeBlock(check.stdout_tail)}`,
        `**错误输出**\n\n${codeBlock(check.stderr_tail)}`,
      ].join('\n\n');
    });
    if (checks.length) sections.push(`## 固定验证\n\n${checks.join('\n\n')}`);
  }

  if (stage.kind === 'build' && typeof result.command === 'string') {
    sections.push([
      '## Server 控制构建',
      `- 命令：\`${result.command}\``,
      `- 目录：\`${String(result.cwd ?? '未记录')}\``,
      `- 退出码：\`${String(result.exit_code ?? '未知')}\``,
      `- 结果：${result.passed === true ? '通过' : '失败'}`,
      `\n**标准输出**\n\n${codeBlock(result.stdout_tail)}`,
      `\n**错误输出**\n\n${codeBlock(result.stderr_tail)}`,
    ].join('\n'));
  }

  if (Array.isArray(result.artifacts) && result.artifacts.length) {
    const rows = result.artifacts.flatMap((item) => {
      if (!item || typeof item !== 'object') return [];
      const artifact = item as Record<string, unknown>;
      return [`- **${String(artifact.partition ?? '未知分区')}** · \`${String(artifact.local_path ?? '')}\` · SHA-256 \`${String(artifact.sha256 ?? '')}\``];
    });
    if (rows.length) sections.push(`## 已验证固件\n\n${rows.join('\n')}`);
  }

  if (stage.kind === 'discuss') {
    if (typeof result.question === 'string') sections.push(`## 你的问题\n\n${result.question}`);
    if (typeof result.answer === 'string' && result.answer.trim()) sections.push(`## Coding Agent 回答\n\n${result.answer}`);
    const evidence = markdownList('核对依据', result.evidence);
    if (evidence) sections.push(evidence);
    const nextSteps = markdownList('建议后续动作', result.suggested_next_steps);
    if (nextSteps) sections.push(nextSteps);
  }

  if (stage.kind === 'merge') {
    sections.push([
      '## 受控主分支合并',
      `- 源分支：\`${String(result.source_branch ?? '未记录')}\``,
      `- 目标分支：\`${String(result.target_branch ?? '未记录')}\``,
      `- 源 commit：\`${String(result.source_commit ?? '未生成')}\``,
      `- 主分支最终 commit：\`${String(result.merge_commit ?? '未生成')}\``,
      `- 远端推送：${result.pushed === true ? '是' : '否'}`,
      `\n**Git 输出**\n\n${codeBlock(result.output)}`,
    ].join('\n'));
  }

  if (typeof result.warning === 'string' && result.warning.trim()) sections.push(`> **提示**\n>\n> ${result.warning}`);
  if (typeof result.error === 'string' && result.error.trim()) sections.push(`## 错误\n\n${codeBlock(result.error)}`);
  if (typeof result.worktree_path === 'string') {
    sections.push(`## 工作区\n\n- 路径：\`${result.worktree_path}\`\n- 分支：\`${String(result.worktree_branch ?? result.work_branch ?? '未记录')}\``);
  }

  if (!sections.length) sections.push(`~~~json\n${JSON.stringify(result, null, 2)}\n~~~`);
  return sections.join('\n\n');
}

function eventMessage(event: RunEvent): string {
  const message = event.message || event.event_type;
  if (!message.trim().startsWith('{')) return message;
  try {
    const payload = JSON.parse(message) as Record<string, unknown>;
    if (typeof payload.summary === 'string') return `Agent 已提交结构化结果：${payload.summary}`;
    return 'Agent 已提交结构化结果，可在“查看产物”中阅读';
  } catch {
    return message;
  }
}

function apiBase(): string {
  if (process.env.NEXT_PUBLIC_DEVELOP_HUB_API_URL) return process.env.NEXT_PUBLIC_DEVELOP_HUB_API_URL;
  if (typeof window !== 'undefined') return `${window.location.protocol}//${window.location.hostname}:8175`;
  return 'http://127.0.0.1:8175';
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${apiBase()}${path}`, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({ detail: response.statusText })) as { detail?: unknown };
    throw new Error(typeof body.detail === 'string' ? body.detail : `请求失败 (${response.status})`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}

function stageState(run: CodingRun, kind: string): 'done' | 'active' | 'waiting' | 'failed' {
  const latest = run.stages.filter((stage) => stage.kind === kind).sort((left, right) => right.sequence - left.sequence)[0];
  if (latest?.status === 'failed') return 'failed';
  if (latest?.status === 'running' || latest?.status === 'awaiting_approval') return 'active';
  if (latest?.status === 'succeeded') return 'done';
  if (kind === run.current_stage) return 'active';
  return 'waiting';
}

function shortThread(value: string | null | undefined): string {
  if (!value) return '等待创建';
  return value.length > 15 ? `${value.slice(0, 8)}…${value.slice(-4)}` : value;
}

function requiresExistingSdkWorkspace(repository: Repository | null | undefined): boolean {
  return Boolean(
    repository?.build_root
    && repository.build_root.replace(/\/+$/, '') !== repository.root_path.replace(/\/+$/, ''),
  );
}

export default function Home() {
  const [view, setView] = useState<'runs' | 'requirements' | 'qa'>('runs');
  const [health, setHealth] = useState<Health | null>(null);
  const [repositories, setRepositories] = useState<Repository[]>([]);
  const [runs, setRuns] = useState<CodingRun[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [dialog, setDialog] = useState<'run' | 'repository-list' | 'repository-form' | null>(null);
  const [artifactStageId, setArtifactStageId] = useState<string | null>(null);
  const [editingRepository, setEditingRepository] = useState<Repository | null>(null);
  const [branchOptions, setBranchOptions] = useState<string[]>([]);
  const [worktreeOptions, setWorktreeOptions] = useState<RepositoryWorktree[]>([]);
  const [runRepositoryId, setRunRepositoryId] = useState('');
  const [worktreeMode, setWorktreeMode] = useState<'new' | 'existing' | 'reuse_new_branch'>('new');
  const [existingWorktreePath, setExistingWorktreePath] = useState('');
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [approvalComment, setApprovalComment] = useState('');
  const [firmwarePartitionInput, setFirmwarePartitionInput] = useState('oem');
  const [codingQuestion, setCodingQuestion] = useState('');
  const [askingCodingAgent, setAskingCodingAgent] = useState(false);
  const [recoveryMessage, setRecoveryMessage] = useState('');
  const [recoveringRun, setRecoveringRun] = useState(false);
  const [followupDraft, setFollowupDraft] = useState<FollowupDraft | null>(null);
  const [mergeReadiness, setMergeReadiness] = useState<MergeReadiness | null>(null);
  const [mergingRun, setMergingRun] = useState(false);

  const selectedRun = useMemo(() => runs.find((run) => run.id === selectedId) || runs[0] || null, [runs, selectedId]);
  const activeRunId = selectedRun?.id || null;
  const repositoryById = useMemo(() => new Map(repositories.map((repo) => [repo.id, repo])), [repositories]);

  const refresh = useCallback(async () => {
    try {
      const [nextHealth, nextRepositories, nextRuns] = await Promise.all([
        request<Health>('/api/health'), request<Repository[]>('/api/repositories'), request<CodingRun[]>('/api/runs'),
      ]);
      setHealth(nextHealth);
      setRepositories(nextRepositories);
      setRuns(nextRuns);
      setSelectedId((current) => current || nextRuns[0]?.id || null);
      setError(null);
    } catch (cause) {
      setHealth(null);
      setError(cause instanceof Error ? cause.message : '无法连接 175 Server');
    }
  }, []);

  const refreshEvents = useCallback(async (runId: string) => {
    try { setEvents(await request<RunEvent[]>(`/api/runs/${runId}/events?limit=200`)); } catch { /* health loop reports connectivity */ }
  }, []);

  const refreshMergeReadiness = useCallback(async (runId: string) => {
    try { setMergeReadiness(await request<MergeReadiness>(`/api/runs/${runId}/merge-readiness`)); }
    catch { setMergeReadiness(null); }
  }, []);

  useEffect(() => {
    const initial = window.setTimeout(() => void refresh(), 0);
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => { window.clearTimeout(initial); window.clearInterval(timer); };
  }, [refresh]);

  useEffect(() => {
    const syncViewFromHash = () => setView(window.location.hash === '#requirements' ? 'requirements' : window.location.hash === '#qa' ? 'qa' : 'runs');
    const initial = window.setTimeout(syncViewFromHash, 0);
    window.addEventListener('hashchange', syncViewFromHash);
    return () => { window.clearTimeout(initial); window.removeEventListener('hashchange', syncViewFromHash); };
  }, []);

  useEffect(() => {
    if (!activeRunId) return;
    const initial = window.setTimeout(() => void refreshEvents(activeRunId), 0);
    const source = new EventSource(`${apiBase()}/api/runs/${activeRunId}/stream`);
    source.onmessage = (message) => {
      const event = JSON.parse(message.data) as RunEvent;
      setEvents((current) => [...current.filter((item) => item.id !== event.id), event].slice(-200));
      void refresh();
    };
    return () => { window.clearTimeout(initial); source.close(); };
  }, [activeRunId, refresh, refreshEvents]);

  useEffect(() => {
    if (!activeRunId) return;
    const initial = window.setTimeout(() => void refreshMergeReadiness(activeRunId), 0);
    const timer = window.setInterval(() => void refreshMergeReadiness(activeRunId), 5000);
    return () => { window.clearTimeout(initial); window.clearInterval(timer); };
  }, [activeRunId, refreshMergeReadiness]);

  async function loadBranches(repositoryId: string) {
    if (!repositoryId) { setBranchOptions([]); return; }
    try {
      const result = await request<RepositoryBranches>(`/api/repositories/${repositoryId}/branches`);
      setBranchOptions(result.branches);
    } catch (cause) {
      setBranchOptions([]);
      setDialogError(cause instanceof Error ? cause.message : '分支读取失败');
    }
  }

  async function loadWorktrees(repositoryId: string, preferredPath?: string) {
    if (!repositoryId) { setWorktreeOptions([]); setExistingWorktreePath(''); return; }
    try {
      const result = await request<RepositoryWorktrees>(`/api/repositories/${repositoryId}/worktrees`);
      setWorktreeOptions(result.worktrees);
      const activePaths = new Set(
        runs
          .filter((run) => !['completed', 'failed', 'cancelled'].includes(run.status) && run.worktree_path)
          .map((run) => run.worktree_path),
      );
      const selectable = result.worktrees.find((worktree) => (
        worktree.path === preferredPath && worktree.branch && !activePaths.has(worktree.path)
      )) || result.worktrees.find((worktree) => worktree.branch && !activePaths.has(worktree.path));
      setExistingWorktreePath(selectable?.path || '');
    } catch (cause) {
      setWorktreeOptions([]); setExistingWorktreePath('');
      setDialogError(cause instanceof Error ? cause.message : 'Worktree 读取失败');
    }
  }

  function openRepositoryList() {
    setEditingRepository(null); setBranchOptions([]); setWorktreeOptions([]); setDialogError(null); setDialog('repository-list');
  }

  function openRepositoryForm(repository: Repository | null) {
    setEditingRepository(repository); setBranchOptions([]); setDialogError(null); setDialog('repository-form');
    if (repository) void loadBranches(repository.id);
  }

  function openRunDialog() {
    const repository = repositories.find((item) => item.enabled) || null;
    const repositoryId = repository?.id || '';
    setFollowupDraft(null); setRunRepositoryId(repositoryId); setBranchOptions([]); setWorktreeOptions([]); setWorktreeMode(requiresExistingSdkWorkspace(repository) ? 'reuse_new_branch' : 'new'); setExistingWorktreePath(''); setDialogError(null); setDialog('run');
    if (repositoryId) { void loadBranches(repositoryId); void loadWorktrees(repositoryId); }
  }

  async function saveRepository(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setDialogError(null);
    const data = new FormData(event.currentTarget);
    try {
      await request(editingRepository ? `/api/repositories/${editingRepository.id}` : '/api/repositories', {
        method: editingRepository ? 'PUT' : 'POST', body: JSON.stringify({
          name: data.get('name'), root_path: data.get('root_path'), default_branch: data.get('default_branch') || 'main',
          validation_commands: String(data.get('validation_commands') || '').split('\n').map((item) => item.trim()).filter(Boolean),
          build_root: String(data.get('build_root') || '').trim() || null,
          firmware_image_root: String(data.get('firmware_image_root') || '').trim() || null,
          ...(editingRepository ? { enabled: data.get('enabled') === 'on' } : {}),
        }),
      });
      await refresh(); setEditingRepository(null); setDialog('repository-list');
    } catch (cause) { setDialogError(cause instanceof Error ? cause.message : '仓库保存失败'); }
    finally { setBusy(false); }
  }

  async function deleteRepository(repository: Repository) {
    const runCount = runs.filter((run) => run.repository_id === repository.id).length;
    if (runCount) {
      setDialogError(`该仓库已有 ${runCount} 条运行记录。为保留审计，请编辑仓库并将其停用。`);
      return;
    }
    const confirmed = window.confirm(
      `确定从 Develop Hub 中删除仓库「${repository.name}」吗？\n\n只会删除平台注册记录，不会删除磁盘目录：\n${repository.root_path}`,
    );
    if (!confirmed) return;
    setBusy(true); setDialogError(null);
    try {
      await request(`/api/repositories/${repository.id}`, { method: 'DELETE' });
      await refresh();
    } catch (cause) { setDialogError(cause instanceof Error ? cause.message : '仓库删除失败'); }
    finally { setBusy(false); }
  }

  async function createRun(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setDialogError(null);
    const data = new FormData(event.currentTarget);
    try {
      const run = await request<CodingRun>('/api/runs', {
        method: 'POST', body: JSON.stringify({
          repository_id: data.get('repository_id'), title: data.get('title'), requirement: data.get('requirement'),
          parent_run_id: followupDraft?.parentRunId || null,
          source_qa_run_id: followupDraft?.sourceQaRunId || null,
          acceptance_criteria: String(data.get('acceptance_criteria') || '').split('\n').map((item) => item.trim()).filter(Boolean),
          source_branch: worktreeMode === 'existing' ? null : data.get('source_branch'),
          worktree_mode: worktreeMode,
          existing_worktree_path: worktreeMode === 'new' ? null : existingWorktreePath,
          auto_start: true,
        }),
      });
      setDialog(null); setFollowupDraft(null); setEvents([]); setSelectedId(run.id); await refresh();
    } catch (cause) { setDialogError(cause instanceof Error ? cause.message : 'Coding Run 创建失败'); }
    finally { setBusy(false); }
  }

  async function decide(approval: Approval, approved: boolean) {
    if (!selectedRun) return;
    setBusy(true); setError(null);
    try {
      await request(`/api/runs/${selectedRun.id}/approvals/${approval.id}`, {
        method: 'POST', body: JSON.stringify({ approved, decided_by: 'local-user', comment: approvalComment.trim() || null }),
      });
      setApprovalComment(''); await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : '审批失败'); }
    finally { setBusy(false); }
  }

  async function submitPlanFeedback(approval: Approval) {
    if (!selectedRun || approval.kind !== 'plan' || !approvalComment.trim()) return;
    setBusy(true); setError(null);
    try {
      await request(`/api/runs/${selectedRun.id}/approvals/${approval.id}/feedback`, {
        method: 'POST', body: JSON.stringify({ message: approvalComment.trim(), submitted_by: 'local-user' }),
      });
      setApprovalComment(''); await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : '方案反馈提交失败'); }
    finally { setBusy(false); }
  }

  async function cancelRun() {
    if (!selectedRun) return;
    setBusy(true);
    try { await request(`/api/runs/${selectedRun.id}/cancel`, { method: 'POST' }); await refresh(); }
    catch (cause) { setError(cause instanceof Error ? cause.message : '取消失败'); }
    finally { setBusy(false); }
  }

  async function retryReview() {
    if (!selectedRun) return;
    setBusy(true); setError(null);
    try {
      await request(`/api/runs/${selectedRun.id}/retry-review`, { method: 'POST' });
      await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : '审查重试失败'); }
    finally { setBusy(false); }
  }

  async function importFirmwareArtifacts() {
    if (!selectedRun) return;
    const partitions = firmwarePartitionInput.split(',').map((item) => item.trim().toLowerCase()).filter(Boolean);
    if (!partitions.length) return;
    setBusy(true); setError(null);
    try {
      await request(`/api/runs/${selectedRun.id}/firmware-artifacts/import`, {
        method: 'POST', body: JSON.stringify({ partitions }),
      });
      await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : '固件镜像登记失败'); }
    finally { setBusy(false); }
  }

  async function askCodingAgent() {
    if (!selectedRun || !codingQuestion.trim()) return;
    setAskingCodingAgent(true); setError(null);
    try {
      await request(`/api/runs/${selectedRun.id}/inquiries`, {
        method: 'POST', body: JSON.stringify({ message: codingQuestion.trim() }),
      });
      setCodingQuestion(''); await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Coding Agent 咨询失败'); }
    finally { setAskingCodingAgent(false); }
  }

  async function recoverCodingRun() {
    if (!selectedRun || !recoveryMessage.trim()) return;
    const confirmed = window.confirm(
      `确定恢复 RUN-${String(selectedRun.display_number).padStart(6, '0')} 吗？\n\nServer 会恢复原 Coding thread，允许修改当前工作区；随后先执行固定验证和独立审查，审查通过后才运行 ./build.sh。\n\n人工说明：${recoveryMessage.trim()}`,
    );
    if (!confirmed) return;
    setRecoveringRun(true); setError(null);
    try {
      await request(`/api/runs/${selectedRun.id}/recover`, {
        method: 'POST', body: JSON.stringify({ message: recoveryMessage.trim() }),
      });
      setRecoveryMessage(''); await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : 'Coding Run 恢复失败'); }
    finally { setRecoveringRun(false); }
  }

  async function mergeSelectedRun() {
    if (!selectedRun || !mergeReadiness?.eligible || !mergeReadiness.qa_run_id) return;
    const confirmed = window.confirm(
      `确定执行本地主分支合并吗？\n\n源分支：${mergeReadiness.source_branch}\n目标分支：${mergeReadiness.target_branch}\n\nServer 会只提交 Coding Agent 声明的交付文件，再执行 fast-forward。不会生成额外 merge commit，也不会 push 到远端。`,
    );
    if (!confirmed) return;
    setMergingRun(true); setError(null);
    try {
      await request(`/api/runs/${selectedRun.id}/merge`, {
        method: 'POST', body: JSON.stringify({
          qa_run_id: mergeReadiness.qa_run_id,
          target_branch: mergeReadiness.target_branch,
          confirmed: true,
        }),
      });
      await Promise.all([refresh(), refreshMergeReadiness(selectedRun.id)]);
    } catch (cause) { setError(cause instanceof Error ? cause.message : '主分支合并失败'); }
    finally { setMergingRun(false); }
  }

  async function deleteRun() {
    if (!selectedRun || !['completed', 'failed', 'cancelled'].includes(selectedRun.status)) return;
    const confirmed = window.confirm(
      `确定删除 RUN-${String(selectedRun.display_number).padStart(6, '0')}「${selectedRun.title}」吗？\n\n运行记录、事件、产物和该任务的 worktree 都会被永久删除，worktree 中未提交的修改无法恢复。`,
    );
    if (!confirmed) return;
    setBusy(true); setError(null);
    try {
      await request(`/api/runs/${selectedRun.id}`, { method: 'DELETE' });
      setEvents([]); setSelectedId(null); await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : '删除失败'); }
    finally { setBusy(false); }
  }

  async function deleteRequirementRecord(run: CodingRun) {
    if (!recordOnlyDeletableStatuses.has(run.status)) return;
    const confirmed = window.confirm(
      `确定仅删除需求记录 REQ-${String(run.display_number).padStart(6, '0')}「${run.title}」吗？\n\n这会删除平台中的需求、运行阶段、审批和事件记录；不会删除 worktree、Git 分支、代码文件或产物目录。`,
    );
    if (!confirmed) return;
    setBusy(true); setError(null);
    try {
      await request(`/api/runs/${run.id}/record-only`, { method: 'DELETE' });
      if (selectedId === run.id) { setEvents([]); setSelectedId(null); }
      await refresh();
    } catch (cause) { setError(cause instanceof Error ? cause.message : '需求记录删除失败'); }
    finally { setBusy(false); }
  }

  const activeCount = runs.filter((run) => !['completed', 'failed', 'cancelled'].includes(run.status)).length;
  const pendingCount = runs.reduce((count, run) => count + run.approvals.filter((item) => item.status === 'pending').length, 0);
  const completedCount = runs.filter((run) => run.status === 'completed').length;
  const pendingApproval = selectedRun?.approvals.find((item) => item.status === 'pending') || null;
  const pendingApprovalStage = selectedRun?.stages.find((stage) => stage.id === pendingApproval?.stage_id) || null;
  const pendingBlockers = stringItems(pendingApprovalStage?.result?.blockers);
  const latestReviewStage = (selectedRun?.stages || []).filter((stage) => stage.kind === 'review').sort((left, right) => right.sequence - left.sequence)[0];
  const canRetryReview = selectedRun?.status === 'failed'
    && latestReviewStage?.status === 'failed'
    && typeof latestReviewStage.result?.error === 'string'
    && selectedRun.stages.some((stage) => stage.kind === 'implement' && stage.status === 'succeeded')
    && selectedRun.stages.some((stage) => stage.kind === 'validate' && stage.status === 'succeeded');
  const codingSession = selectedRun?.sessions.find((item) => item.role === 'coding');
  const codingDiscussions = (selectedRun?.stages || []).filter((stage) => stage.kind === 'discuss').sort((left, right) => left.sequence - right.sequence);
  const codingInquiryRunning = codingDiscussions.some((stage) => stage.status === 'running');
  const artifactStages = useMemo(
    () => (selectedRun?.stages || []).filter((stage) => stage.result).sort((left, right) => left.sequence - right.sequence),
    [selectedRun],
  );
  const selectedArtifactStage = artifactStages.find((stage) => stage.id === artifactStageId) || artifactStages.at(-1) || null;
  const recentEvents = events.slice(-5).reverse();
  const enabledRepositories = repositories.filter((repository) => repository.enabled);
  const runRepository = repositories.find((repository) => repository.id === runRepositoryId) || null;
  const activeWorktreePaths = new Set(
    runs
      .filter((run) => !['completed', 'failed', 'cancelled'].includes(run.status) && run.worktree_path)
      .map((run) => run.worktree_path),
  );
  const selectableWorktrees = worktreeOptions.filter(
    (worktree) => worktree.branch && !activeWorktreePaths.has(worktree.path),
  );
  const selectedWorktree = selectableWorktrees.find((worktree) => worktree.path === existingWorktreePath) || null;

  function openArtifactViewer(preferredKind?: string) {
    const preferred = preferredKind ? artifactStages.filter((stage) => stage.kind === preferredKind).at(-1) : null;
    const stage = preferred || artifactStages.at(-1);
    if (stage) setArtifactStageId(stage.id);
  }

  function openRun(runId: string) {
    setEvents([]); setApprovalComment(''); setSelectedId(runId); setView('runs');
    window.history.pushState(null, '', '#runs');
  }

  return (
    <main className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark">D</span><div><strong>Develop Hub</strong><span>175 Control Plane</span></div></div>
        <nav aria-label="主导航">
          <p className="nav-label">工作空间</p>
          <a className={`nav-item ${view === 'runs' ? 'active' : ''}`} href="#runs" onClick={() => setView('runs')}><span>⌁</span>研发运行</a>
          <a className={`nav-item ${view === 'requirements' ? 'active' : ''}`} href="#requirements" onClick={() => setView('requirements')}><span>◇</span>需求池</a>
          <a className={`nav-item ${view === 'qa' ? 'active' : ''}`} href="#qa" onClick={() => setView('qa')}><span>▣</span>测试台</a>
          <a className="nav-item" href="#repositories" onClick={(event) => { event.preventDefault(); openRepositoryList(); }}><span>⌘</span>代码仓库</a>
          <p className="nav-label">控制平面</p>
          <a className="nav-item" href="#agents"><span>●</span>执行节点</a>
          <a className="nav-item" href="#knowledge"><span>▱</span>规则与知识</a>
        </nav>
        {view !== 'qa' && <div className="run-selector">
          <p className="nav-label">最近运行</p>
          {runs.slice(0, 5).map((run) => (
            <button className={run.id === selectedRun?.id && view === 'runs' ? 'selected' : ''} key={run.id} onClick={() => openRun(run.id)}>
              <span>#{String(run.display_number).padStart(4, '0')}</span>{run.title}
            </button>
          ))}
        </div>
        }
        <div className="node-card">
          <div className="node-card-title"><span className={`status-dot ${health ? '' : 'offline'}`} />175 Runner {health ? '在线' : '离线'}</div>
          <p>{health?.codex_version || '等待连接'}</p><div className="meter"><span style={{ width: health ? `${Math.min(100, health.active_jobs * 35)}%` : '0%' }} /></div>
          <small>{health ? `${health.active_jobs} 个研发任务运行中 · ${health.database}` : '检查 8175 端口'}</small>
        </div>
      </aside>

      <section className="content">
        <header className="topbar"><div><span className="eyebrow">{view === 'requirements' ? 'REQUIREMENT POOL' : view === 'qa' ? 'TEST CONTROL' : 'CODING CONTROL'}</span><h1>{view === 'requirements' ? '需求池' : view === 'qa' ? '测试平台' : '研发运行'}</h1></div>
          {view !== 'qa' && <div className="topbar-actions"><button className="secondary-button" onClick={openRepositoryList}>管理仓库</button><button className="primary-button" onClick={openRunDialog} disabled={!enabledRepositories.length}><span>＋</span>新建 Coding Run</button></div>}
        </header>
        {error && view !== 'qa' && <div className="error-banner"><span>{error}</span><button onClick={() => setError(null)}>×</button></div>}

        {view === 'qa' ? <QaWorkbench onManageRepositories={openRepositoryList} /> : view === 'requirements' ? <section className="requirement-pool" id="requirements" aria-labelledby="requirement-pool-title">
          <div className="requirement-pool-heading"><div><span className="panel-label">已进入控制流程的需求</span><h2 id="requirement-pool-title">全部需求</h2><p>每个需求对应一个 Coding Run，保留原始背景、验收条件和当前交付状态。</p></div><span>{runs.length} 条需求</span></div>
          {runs.length ? <div className="requirement-grid">{runs.map((run) => {
            const repository = repositoryById.get(run.repository_id);
            return <article className="panel requirement-item" key={run.id}><header><span className="run-id">REQ-{String(run.display_number).padStart(6, '0')}</span><div className="requirement-tags"><span className={`lifecycle-tag ${run.closed_at ? 'closed' : 'open'}`}>{run.closed_at ? 'CLOSED' : 'OPEN'}</span><span className={`requirement-status status-${run.status}`}>{statusLabels[run.status] || run.status}</span></div></header><h3>{run.title}</h3><p className="requirement-meta">{repository?.name || '未注册仓库'} · {new Date(run.created_at).toLocaleString('zh-CN', { hour12: false })}</p><p className="requirement-summary">{run.requirement}</p>{run.acceptance_criteria.length > 0 && <div className="requirement-acceptance"><strong>验收条件</strong><ul>{run.acceptance_criteria.slice(0, 3).map((criterion) => <li key={criterion}>{criterion}</li>)}</ul>{run.acceptance_criteria.length > 3 && <small>另有 {run.acceptance_criteria.length - 3} 项</small>}</div>}<footer><span>{run.closed_at ? `已关闭 · ${new Date(run.closed_at).toLocaleString('zh-CN', { hour12: false })}` : run.approvals.some((approval) => approval.status === 'pending') ? '等待人工确认' : `当前阶段 · ${stageKindLabels[run.current_stage] || run.current_stage}`}</span><div className="requirement-actions"><button className="requirement-delete" type="button" disabled={busy || !recordOnlyDeletableStatuses.has(run.status)} title={recordOnlyDeletableStatuses.has(run.status) ? '仅删除平台记录' : '运行中的需求不能删除'} onClick={() => void deleteRequirementRecord(run)}>删除记录</button><button type="button" onClick={() => openRun(run.id)}>打开运行 →</button></div></footer></article>;
          })}</div> : <section className="empty-state panel"><span className="empty-mark">00</span><h2>需求池为空</h2><p>创建 Coding Run 后，需求背景和验收条件会自动进入这里。</p><div><button className="primary-button" disabled={!enabledRepositories.length} onClick={openRunDialog}>新建 Coding Run</button></div></section>}
        </section> : <>
        <section className="summary-grid" aria-label="运行概览">
          <article className="summary-card accent"><div><span>进行中的流程</span><strong>{String(activeCount).padStart(2, '0')}</strong></div><p>175 coding control queue</p></article>
          <article className="summary-card"><div><span>等待人工确认</span><strong>{String(pendingCount).padStart(2, '0')}</strong></div><p>方案审批 · 完成门禁</p></article>
          <article className="summary-card"><div><span>已完成运行</span><strong>{String(completedCount).padStart(2, '0')}</strong></div><p><b>{runs.length}</b> 个记录已纳入审计</p></article>
          <article className="summary-card"><div><span>Agent 健康度</span><strong className={health ? 'healthy' : 'unhealthy'}>{health ? '正常' : '离线'}</strong></div><p>{health ? `${health.node_id} · ${health.active_jobs} active` : '无法连接 Server'}</p></article>
        </section>

        {!selectedRun ? (
          <section className="empty-state panel"><span className="empty-mark">01</span><h2>创建第一个 Coding Run</h2><p>先注册 175 上的 Git 仓库及固定验证命令，然后提交需求。平台将创建隔离 worktree 并启动规划 Agent。</p>
            <div><button className="secondary-button" onClick={() => openRepositoryForm(null)}>注册代码仓库</button><button className="primary-button" disabled={!enabledRepositories.length} onClick={openRunDialog}>新建 Coding Run</button></div>
          </section>
        ) : (
          <section className="workspace-grid" id="runs">
            <article className="panel run-panel">
              <div className="panel-heading"><div><span className="run-id">RUN-{String(selectedRun.display_number).padStart(6, '0')} · {selectedRun.closed_at ? 'CLOSED' : 'OPEN'}</span><h2>{selectedRun.title}</h2><p>{repositoryById.get(selectedRun.repository_id)?.name || 'repository'} · {selectedRun.worktree_branch || selectedRun.work_branch}</p></div>
                <div className="run-actions">{artifactStages.length > 0 && <button className="artifact-button" type="button" onClick={() => openArtifactViewer()}>查看产物</button>}<span className={`running-pill status-${selectedRun.status}`}><i />{statusLabels[selectedRun.status] || selectedRun.status}</span>{canRetryReview && <button className="approve" disabled={busy} onClick={() => void retryReview()}>{busy ? '正在重试…' : '重试审查'}</button>}{!['completed', 'failed', 'cancelled'].includes(selectedRun.status) && <button className="icon-button" disabled={busy} onClick={cancelRun}>取消</button>}{['completed', 'failed', 'cancelled'].includes(selectedRun.status) && <button className="danger-button" disabled={busy} onClick={deleteRun}>删除运行</button>}</div>
              </div>
              <div className="pipeline">
                {pipeline.map(([kind, label], index) => { const state = stageState(selectedRun, kind); return (
                  <div className={`stage ${state}`} key={kind}><span className="stage-index">{state === 'done' ? '✓' : state === 'failed' ? '!' : index + 1}</span><strong>{label}</strong>{index < pipeline.length - 1 && <i className="stage-line" />}</div>
                ); })}
              </div>
              <div className="current-task"><div className="task-icon">C</div><div className="task-copy"><span>当前阶段</span><strong>{statusLabels[selectedRun.status] || selectedRun.status}</strong><p>thread {shortThread(codingSession?.codex_thread_id)} · {selectedRun.worktree_path || 'worktree 准备中'}</p></div><div className="task-time"><span>修复循环</span><strong>{selectedRun.repair_attempts} / 2</strong></div></div>
              <section className="requirement-card" aria-labelledby="requirement-title"><div className="requirement-heading"><span id="requirement-title">需求背景</span><small>创建于 {new Date(selectedRun.created_at).toLocaleString('zh-CN', { hour12: false })}</small></div><p>{selectedRun.requirement}</p>{selectedRun.acceptance_criteria.length > 0 && <div className="acceptance-list"><strong>验收条件</strong><ul>{selectedRun.acceptance_criteria.map((criterion, index) => <li key={`${index}-${criterion}`}>{criterion}</li>)}</ul></div>}</section>
              {selectedRun.last_error && <div className="run-error"><strong>流程错误</strong><p>{selectedRun.last_error}</p></div>}
              <div className="event-table"><div className="event-head"><span>实时事件</span><button type="button" onClick={() => void refreshEvents(selectedRun.id)}>刷新日志 ↻</button></div>
                {recentEvents.length ? recentEvents.map((event) => <div className="event-row" key={event.id}><time>{new Date(event.created_at).toLocaleTimeString('zh-CN', { hour12: false })}</time><span title={eventMessage(event)}>{eventMessage(event)}</span><b className={event.event_type.includes('failed') ? 'live' : ''}>{event.event_type.split('.').at(-1)}</b></div>) : <p className="quiet-row">等待第一个运行事件…</p>}
              </div>
            </article>

            <aside className="right-column">
              {pendingApproval ? <article className="panel approval-card">
                <div className="panel-label">需要你的确认 · {pendingApproval.kind}</div>
                <h3>{pendingApproval.kind === 'plan' ? (pendingBlockers.length ? 'Agent 需要你补充信息' : '设计方案已准备就绪') : '确认固件交付后结束 Coding Run'}</h3>
                <div className="approval-summary"><ReactMarkdown remarkPlugins={[remarkGfm]}>{pendingApproval.summary}</ReactMarkdown></div>
                {pendingBlockers.length > 0 && <div className="approval-questions"><strong>待确认问题</strong><ul>{pendingBlockers.map((blocker) => <li key={blocker}>{blocker}</li>)}</ul></div>}
                {pendingApproval.kind === 'completion' && <section className="firmware-gate">
                  <strong>175 固件交付</strong>
                  {selectedRun.firmware_artifacts.length ? selectedRun.firmware_artifacts.map((artifact) => <div className="firmware-artifact" key={artifact.id}><span>{artifact.partition}</span><b>{artifact.filename}</b><small>{artifact.source === 'controlled_build' ? 'Server ./build.sh' : '人工导入'} · {(artifact.size_bytes / 1024 / 1024).toFixed(1)} MiB · SHA-256 {artifact.sha256.slice(0, 16)}…</small><code title={artifact.local_path}>{artifact.local_path}</code></div>) : <><p>尚无 Server 校验过的镜像，不能批准完成。当前任务已人工编译时，可从仓库配置的镜像目录登记。</p><label><span>待烧录分区</span><input value={firmwarePartitionInput} onChange={(event) => setFirmwarePartitionInput(event.target.value)} placeholder="oem 或 oem,boot,uboot" /></label><button className="revision-button" type="button" disabled={busy || !firmwarePartitionInput.trim()} onClick={() => void importFirmwareArtifacts()}>{busy ? '正在校验…' : '登记并校验现有镜像'}</button></>}
                </section>}
                <button className="approval-artifact-button" type="button" onClick={() => openArtifactViewer(pendingApproval.kind === 'plan' ? 'plan' : undefined)}>查看完整{pendingApproval.kind === 'plan' ? '方案' : '产物'} →</button>
                <div className="change-meta"><span>{selectedRun.stages.length} 个阶段记录</span><span>{selectedRun.repair_attempts} 次修复</span></div>
                <label className="approval-comment"><span>{pendingApproval.kind === 'plan' ? '补充信息或修改意见' : '审批备注'}</span><textarea value={approvalComment} maxLength={8000} rows={4} onChange={(event) => setApprovalComment(event.target.value)} placeholder={pendingApproval.kind === 'plan' ? '回答 Agent 的问题，或说明需要调整的方案内容…' : '可选：记录本次确认意见…'} /></label>
                {pendingApproval.kind === 'plan' && <button className="revision-button" type="button" disabled={busy || !approvalComment.trim()} onClick={() => void submitPlanFeedback(pendingApproval)}>{busy ? '正在提交…' : '提交反馈，让 Agent 重新出方案'}</button>}
                <div className="approval-actions"><button disabled={busy} onClick={() => void decide(pendingApproval, false)}>拒绝并暂停</button><button className="approve" disabled={busy || (pendingApproval.kind === 'completion' && !selectedRun.firmware_artifacts.length)} onClick={() => void decide(pendingApproval, true)}>批准并继续</button></div>
              </article>
                : <><article className="panel approval-card quiet"><div className="panel-label">流程门禁</div><h3>当前无需人工确认</h3><p>{selectedRun.status === 'completed' ? 'Coding Run 已完成；固件交付仍会保留在下方。' : 'Agent 和固定验证步骤正在执行。到达方案或完成门禁时，操作会出现在这里。'}</p></article>{selectedRun.status === 'completed' && <article className="panel approval-card quiet"><div className="panel-label">Coding → QA 固件交付</div><h3>{selectedRun.firmware_artifacts.length ? '已登记 Server 校验镜像' : '补录完成前人工编译的镜像'}</h3><section className="firmware-gate">{selectedRun.firmware_artifacts.length ? selectedRun.firmware_artifacts.map((artifact) => <div className="firmware-artifact" key={artifact.id}><span>{artifact.partition}</span><b>{artifact.filename}</b><small>{artifact.source === 'controlled_build' ? 'Server ./build.sh' : '人工导入'} · {(artifact.size_bytes / 1024 / 1024).toFixed(1)} MiB · SHA-256 {artifact.sha256.slice(0, 16)}…</small><code title={artifact.local_path}>{artifact.local_path}</code></div>) : <><p>只会从仓库配置的镜像目录读取、计算 SHA-256 并登记；不会自动传输或烧录。</p><label><span>待烧录分区</span><input value={firmwarePartitionInput} onChange={(event) => setFirmwarePartitionInput(event.target.value)} placeholder="oem 或 oem,boot,uboot" /></label><button className="revision-button" type="button" disabled={busy || !firmwarePartitionInput.trim()} onClick={() => void importFirmwareArtifacts()}>{busy ? '正在校验…' : '登记并校验现有镜像'}</button></>}</section></article>}</>}
              {codingSession?.codex_thread_id && ['failed', 'blocked'].includes(selectedRun.status) && <article className="panel coding-recovery-card">
                <div className="panel-label">CODING AGENT · 操作恢复</div>
                <h3>告诉 Agent 处理结果并继续流程</h3>
                <p>精确恢复 thread {shortThread(codingSession.codex_thread_id)}。Agent 会先核验你的说明，可继续修改当前工作区；随后 Server 先做固定验证和独立审查，审查通过后才执行 <code>./build.sh</code>。</p>
                <div className="recovery-warning"><strong>恢复前错误</strong><span>{selectedRun.last_error || '未记录错误'}</span></div>
                <label><span>处理信息或继续指令</span><textarea rows={4} maxLength={12000} value={recoveryMessage} onChange={(event) => setRecoveryMessage(event.target.value)} placeholder="例如：我已在 SDK 的 kernel 仓库应用指定 patch，请核验 DW3C 配置，完成工作区内剩余修改并继续构建与审查。" /></label>
                <button className="approve" type="button" disabled={recoveringRun || codingInquiryRunning || !recoveryMessage.trim()} onClick={() => void recoverCodingRun()}>{codingInquiryRunning ? '等待只读咨询结束…' : recoveringRun ? '正在恢复…' : '提交信息并继续执行'}</button>
              </article>}
              {codingSession?.codex_thread_id && <article className="panel coding-chat"><div className="panel-label">CODING AGENT · 只读咨询</div><h3>继续询问原开发 Agent</h3><p className="coding-chat-hint">精确恢复 thread {shortThread(codingSession.codex_thread_id)}；本模式不允许修改代码，也不会推进流程。</p><div className="coding-chat-history">{codingDiscussions.length ? codingDiscussions.slice(-5).map((stage) => <div className="coding-exchange" key={stage.id}><div><strong>你</strong><p>{String(stage.result?.question || '未记录问题')}</p></div><div><strong>Coding Agent</strong>{stage.status === 'running' ? <p className="chat-pending">正在核对代码和证据…</p> : stage.status === 'failed' ? <p className="chat-error">{String(stage.result?.error || '咨询失败')}</p> : <div className="markdown-body"><ReactMarkdown remarkPlugins={[remarkGfm]}>{String(stage.result?.answer || '未生成回答')}</ReactMarkdown>{stage.result?.requires_code_change === true && <span className="change-needed">建议转为后续 Coding Run</span>}</div>}</div></div>) : <p>可以询问实现细节、变更边界，或让 Agent 根据 QA 现象判断是否需要修改代码。</p>}</div><label><span>继续提问</span><textarea rows={3} maxLength={12000} value={codingQuestion} onChange={(event) => setCodingQuestion(event.target.value)} placeholder="例如：QA 发现 RGB 帧头 exposure_us 仍为 0，请核对可能原因和修改点…" /></label><button className="approve" type="button" disabled={askingCodingAgent || codingInquiryRunning || !codingQuestion.trim() || !['awaiting_completion_approval', 'completed', 'failed', 'blocked'].includes(selectedRun.status)} onClick={() => void askCodingAgent()}>{codingInquiryRunning ? 'Agent 回答中…' : '询问 Coding Agent'}</button></article>}
              {selectedRun.status === 'completed' && mergeReadiness && <article className={`panel coding-merge-card merge-${mergeReadiness.status}`}><div className="panel-label">LOCAL MERGE GATE</div><h3>{mergeReadiness.status === 'succeeded' ? `已合并到 ${mergeReadiness.target_branch}` : mergeReadiness.status === 'running' ? '正在执行受控合并' : '主分支合并'}</h3><p>{mergeReadiness.reason}</p><dl><div><dt>源分支</dt><dd>{mergeReadiness.source_branch}</dd></div><div><dt>目标分支</dt><dd>{mergeReadiness.target_branch}</dd></div></dl>{mergeReadiness.status === 'succeeded' && <small>integrated commit · {String(mergeReadiness.merge_result?.merge_commit || '').slice(0, 12)} · 未推送远端</small>}<button className="approve" type="button" disabled={!mergeReadiness.eligible || mergingRun} onClick={() => void mergeSelectedRun()}>{mergeReadiness.status === 'failed' ? '修正后重试合并' : mergingRun || mergeReadiness.status === 'running' ? '正在合并…' : '确认合并到主分支'}</button></article>}
              <article className="panel context-card"><div className="panel-label">运行上下文</div><dl><div><dt>执行节点</dt><dd>{codingSession?.node_id || '175-coding'}</dd></div><div><dt>工作目录</dt><dd>{selectedRun.worktree_path || '准备中'}</dd></div><div><dt>工作区模式</dt><dd>{selectedRun.worktree_mode === 'existing' ? '现有 worktree / 当前分支' : selectedRun.worktree_mode === 'reuse_new_branch' ? '现有 worktree / 新建分支' : '新建隔离 worktree'}</dd></div><div><dt>源分支</dt><dd>{selectedRun.source_branch}</dd></div><div><dt>开发分支</dt><dd>{selectedRun.worktree_branch || selectedRun.source_branch}</dd></div><div><dt>Sandbox</dt><dd>workspace-write</dd></div><div><dt>Thread</dt><dd>{shortThread(codingSession?.codex_thread_id)}</dd></div></dl></article>
            </aside>
          </section>
        )}
        </>}
      </section>

      {artifactStageId && selectedRun && <div className="dialog-backdrop artifact-backdrop" role="presentation" onMouseDown={() => setArtifactStageId(null)}><section className="dialog artifact-dialog" role="dialog" aria-modal="true" aria-labelledby="artifact-dialog-title" onMouseDown={(event) => event.stopPropagation()}>
        <div className="dialog-heading artifact-heading"><div><span className="eyebrow">RUN-{String(selectedRun.display_number).padStart(6, '0')} · OUTPUTS</span><h2 id="artifact-dialog-title">方案与产物</h2><p>{selectedRun.title}</p></div><button onClick={() => setArtifactStageId(null)} aria-label="关闭产物窗口">×</button></div>
        <div className="artifact-layout">
          <nav className="artifact-nav" aria-label="阶段产物">
            <span className="artifact-nav-label">阶段记录</span>
            {artifactStages.map((stage) => <button className={stage.id === selectedArtifactStage?.id ? 'active' : ''} type="button" key={stage.id} onClick={() => setArtifactStageId(stage.id)}><span>{String(stage.sequence).padStart(2, '0')}</span><strong>{stageKindLabels[stage.kind] || stage.kind}</strong><small className={`artifact-status status-${stage.status}`}>{stageStatusLabels[stage.status] || stage.status}</small></button>)}
          </nav>
          <article className="artifact-content">
            {selectedArtifactStage ? <><header><div><span className="panel-label">阶段 {String(selectedArtifactStage.sequence).padStart(2, '0')}</span><h3>{stageKindLabels[selectedArtifactStage.kind] || selectedArtifactStage.kind}</h3></div><span className={`artifact-status status-${selectedArtifactStage.status}`}>{stageStatusLabels[selectedArtifactStage.status] || selectedArtifactStage.status}</span></header><div className="markdown-body"><ReactMarkdown remarkPlugins={[remarkGfm]}>{stageResultMarkdown(selectedArtifactStage)}</ReactMarkdown></div><details className="raw-artifact"><summary>查看原始 JSON（审计数据）</summary><pre>{JSON.stringify(selectedArtifactStage.result, null, 2)}</pre></details></> : <p className="artifact-empty">暂无阶段产物。</p>}
          </article>
        </div>
      </section></div>}

      {dialog && <div className="dialog-backdrop" role="presentation" onMouseDown={() => !busy && setDialog(null)}><section className={`dialog ${dialog === 'repository-list' ? 'repository-dialog' : ''}`} role="dialog" aria-modal="true" aria-labelledby="dialog-title" onMouseDown={(event) => event.stopPropagation()}>
        <div className="dialog-heading"><div><span className="eyebrow">175 CONTROL PLANE</span><h2 id="dialog-title">{dialog === 'run' ? (followupDraft ? '创建 QA 后续 Coding Run' : '新建 Coding Run') : dialog === 'repository-list' ? '代码仓库管理' : editingRepository ? '编辑代码仓库' : '注册代码仓库'}</h2></div><button onClick={() => setDialog(null)} aria-label="关闭">×</button></div>
        {dialogError && <div className="dialog-error"><span>{dialogError}</span><button type="button" onClick={() => setDialogError(null)}>×</button></div>}
        {dialog === 'run' ? <form onSubmit={createRun}>
          {followupDraft && <div className="followup-banner"><strong>已关联 QA 反馈</strong><span>新任务会保留父任务和 QA 来源；任何代码修改仍需重新经过完整门禁。</span></div>}
          <label>代码仓库<select name="repository_id" required value={runRepositoryId} onChange={(event) => { const repositoryId = event.target.value; const repository = repositories.find((item) => item.id === repositoryId); setRunRepositoryId(repositoryId); setWorktreeMode(requiresExistingSdkWorkspace(repository) ? 'reuse_new_branch' : 'new'); setDialogError(null); void loadBranches(repositoryId); void loadWorktrees(repositoryId); }}>{enabledRepositories.map((repository) => <option value={repository.id} key={repository.id}>{repository.name}</option>)}</select></label>
          <fieldset className="workspace-mode"><legend>工作目录</legend><label className={worktreeMode === 'new' ? 'selected' : ''}><input type="radio" name="workspace_location" value="new" checked={worktreeMode === 'new'} disabled={requiresExistingSdkWorkspace(runRepository)} onChange={() => setWorktreeMode('new')} /><span><strong>新建隔离 Worktree</strong><small>{requiresExistingSdkWorkspace(runRepository) ? '当前仓库嵌套在 SDK 中，./build.sh 要求使用现有 SDK 工作区' : '创建独立目录，适合多个任务并行开发'}</small></span></label><label className={worktreeMode !== 'new' ? 'selected' : ''}><input type="radio" name="workspace_location" value="existing" checked={worktreeMode !== 'new'} onChange={() => setWorktreeMode('reuse_new_branch')} /><span><strong>使用已有 Worktree</strong><small>先选择目录，再在下面选择分支处理方式</small></span></label></fieldset>
          {worktreeMode !== 'new' && <><label>Worktree <small>已被其他活跃 Run 占用的目录不会显示</small><select required value={existingWorktreePath} onChange={(event) => setExistingWorktreePath(event.target.value)}>{selectableWorktrees.length ? <>{selectableWorktrees.filter((worktree) => worktree.is_main).map((worktree) => <option value={worktree.path} key={worktree.path}>主工作区 · {worktree.path}</option>)}{selectableWorktrees.some((worktree) => !worktree.is_main) && <optgroup label="其他 Worktree">{selectableWorktrees.filter((worktree) => !worktree.is_main).map((worktree) => <option value={worktree.path} key={worktree.path}>{worktree.path}</option>)}</optgroup>}</> : <option value="">没有可用的 worktree</option>}</select><span className="field-hint">当前分支：<b>{selectedWorktree?.branch || '未选择'}</b></span></label><label>开发分支 <small>选择如何使用上面的 worktree</small><select value={worktreeMode} onChange={(event) => setWorktreeMode(event.target.value as 'existing' | 'reuse_new_branch')}><option value="reuse_new_branch">从指定源分支新建任务分支</option><option value="existing">继续当前分支{selectedWorktree?.branch ? ` · ${selectedWorktree.branch}` : ''}</option></select><span className="field-hint">{worktreeMode === 'reuse_new_branch' ? '允许保留未跟踪文件；tracked/staged 修改或冲突会阻止 checkout。' : '不会切换分支，Agent 直接在当前分支修改。'}</span></label></>}
          {worktreeMode !== 'existing' && <label>{worktreeMode === 'reuse_new_branch' ? '新分支起点' : '源分支'} <small>创建前会在 175 仓库中解析到具体 commit</small><input name="source_branch" list="run-branches" required defaultValue={followupDraft?.sourceBranch || runRepository?.default_branch || ''} key={`${runRepositoryId}-${worktreeMode}-${followupDraft?.sourceBranch || ''}`} /><datalist id="run-branches">{branchOptions.map((branch) => <option value={branch} key={branch} />)}</datalist>{runRepository && <span className="field-hint">仓库默认分支：{runRepository.default_branch} · 已发现 {branchOptions.length} 个分支</span>}</label>}
          <label>任务标题<input name="title" required minLength={3} defaultValue={followupDraft?.title || ''} placeholder="例如：完善 OTA 升级失败恢复机制" /></label><label>需求描述<textarea name="requirement" required minLength={10} rows={followupDraft ? 12 : 6} defaultValue={followupDraft?.requirement || ''} placeholder="描述背景、预期行为、限制条件…" /></label><label>验收条件 <small>每行一项</small><textarea name="acceptance_criteria" rows={followupDraft ? 6 : 4} defaultValue={followupDraft?.acceptanceCriteria.join('\n') || ''} placeholder={'升级失败时自动回滚\n重启后可恢复升级状态'} /></label><div className="form-actions"><button type="button" onClick={() => { setFollowupDraft(null); setDialog(null); }}>取消</button><button className="approve" disabled={busy || (worktreeMode !== 'new' && !existingWorktreePath)} type="submit">{busy ? '正在校验工作区…' : '校验并启动规划'}</button></div></form>
          : dialog === 'repository-form' ? <form onSubmit={saveRepository}>
            <label>仓库名称<input name="name" required defaultValue={editingRepository?.name || ''} placeholder="sdk-platform" /></label><label>175 本地绝对路径<input name="root_path" required defaultValue={editingRepository?.root_path || ''} placeholder="/home/nwj/project/sdk-platform" /></label><label>默认分支 <small>保存前必须解析到有效 commit</small><input name="default_branch" list="repository-branches" defaultValue={editingRepository?.default_branch || 'main'} required /><datalist id="repository-branches">{branchOptions.map((branch) => <option value={branch} key={branch} />)}</datalist>{editingRepository && <span className="field-hint">当前仓库发现 {branchOptions.length} 个分支</span>}</label><label>SDK 构建根目录 <small>审查通过后 Server 固定在此执行 ./build.sh</small><input name="build_root" defaultValue={editingRepository?.build_root || ''} placeholder="/home/nwj/RV1126B_STRUCTURE_LIGHT" /></label><label>固件镜像目录 <small>必须位于 SDK 构建根目录内</small><input name="firmware_image_root" defaultValue={editingRepository?.firmware_image_root || ''} placeholder="/home/nwj/RV1126B_STRUCTURE_LIGHT/output/image" /></label><label>固定验证命令 <small>独立审查前执行，每行一条</small><textarea name="validation_commands" rows={5} defaultValue={editingRepository?.validation_commands.join('\n') || ''} placeholder={'git diff --check\n./run_tests.sh'} /></label>{editingRepository && <label className="checkbox-field"><input name="enabled" type="checkbox" defaultChecked={editingRepository.enabled} />允许创建新的 Coding Run</label>}<div className="form-actions"><button type="button" onClick={() => editingRepository ? openRepositoryList() : setDialog(null)}>取消</button><button className="approve" disabled={busy} type="submit">{busy ? '正在验证…' : editingRepository ? '验证并保存' : '验证并注册'}</button></div></form>
            : <div className="repository-list"><div className="repository-list-head"><p>注册、校验并维护 175 上的本地 Git 仓库。删除注册记录不会删除磁盘目录。</p><button className="primary-button" type="button" onClick={() => openRepositoryForm(null)}>＋ 注册仓库</button></div>{repositories.length ? repositories.map((repository) => { const runCount = runs.filter((run) => run.repository_id === repository.id).length; return <article className="repository-row" key={repository.id}><div><div className="repository-title"><strong>{repository.name}</strong><span className={repository.enabled ? 'repo-enabled' : 'repo-disabled'}>{repository.enabled ? '已启用' : '已停用'}</span></div><p>{repository.root_path}</p><small>默认分支 <b>{repository.default_branch}</b> · {repository.validation_commands.length} 条验证命令 · {runCount ? `${runCount} 条运行记录` : '未使用'}</small></div><div className="repository-actions"><button className="secondary-button" type="button" onClick={() => openRepositoryForm(repository)}>编辑</button><button className="repository-delete" type="button" disabled={busy || runCount > 0} title={runCount ? '仓库有运行记录，请停用以保留审计' : '删除平台注册记录'} onClick={() => void deleteRepository(repository)}>删除</button></div></article>; }) : <p className="repository-empty">尚未注册仓库。</p>}</div>}
      </section></div>}
    </main>
  );
}
