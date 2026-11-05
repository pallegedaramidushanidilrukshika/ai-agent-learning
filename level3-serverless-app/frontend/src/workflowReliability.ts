export type StartRequestPayload = {
  repository_path: string
  bug_title: string
  bug_description: string
  allow_local_commands: boolean
  allow_ai: boolean
  manual_reproduction?: {
    steps: string[]
    expected_behavior: string
    observed_behavior: string
    supporting_evidence: string[]
  }
  attachments?: Array<{ name: string; content: string }>
}

export type StartRequest = {
  requestId: string
  payload: StartRequestPayload
}

export type WorkflowStatusResult =
  | { ok: true; workflow: unknown }
  | { ok: false; error: string }

const TERMINAL_STATUSES = new Set(['PASS', 'FAIL', 'FAILED', 'STOPPED', 'BLOCKED_FINAL', 'UNCONFIRMED'])

// The backend sets `terminal` once the workflow process has exited. A report
// file alone is not enough: it can appear before the final status is written.
export function isTerminalWorkflow(
  workflow: { status?: string; terminal?: boolean } | null | undefined,
): boolean {
  if (!workflow) return false
  if (typeof workflow.terminal === 'boolean') return workflow.terminal
  return TERMINAL_STATUSES.has(workflow.status ?? '')
}

// Same limits as the backend (evidence_files.py); checked here so the user
// gets an immediate message.
export const EVIDENCE_EXTENSIONS = ['.har', '.json', '.log', '.txt', '.js', '.cjs', '.mjs', '.ts', '.py']
export const MAX_EVIDENCE_FILES = 5
export const MAX_EVIDENCE_FILE_BYTES = 256 * 1024
export const MAX_EVIDENCE_TOTAL_BYTES = 1024 * 1024

export function evidenceFilesError(
  files: Array<{ name: string; size: number }>,
  existing: Array<{ bytes: number }> = [],
): string | null {
  if (files.length === 0) return 'Choose at least one file.'
  if (files.length + existing.length > MAX_EVIDENCE_FILES) return `At most ${MAX_EVIDENCE_FILES} evidence files can be attached to one run.`
  for (const file of files) {
    const dot = file.name.lastIndexOf('.')
    const extension = dot >= 0 ? file.name.slice(dot).toLowerCase() : ''
    if (!EVIDENCE_EXTENSIONS.includes(extension)) return `${file.name}: attach .har, .json, .log, .txt, .js, .ts, .cjs, .mjs or .py files.`
    if (file.size === 0) return `${file.name}: the file is empty.`
    if (file.size > MAX_EVIDENCE_FILE_BYTES) return `${file.name}: larger than 256 KB.`
  }
  const total = files.reduce((sum, file) => sum + file.size, 0) + existing.reduce((sum, item) => sum + item.bytes, 0)
  if (total > MAX_EVIDENCE_TOTAL_BYTES) return 'Evidence for one run must total at most 1 MB.'
  return null
}

export class ApiResponseError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

export function getStartRequest(
  existing: StartRequest | null,
  payload: StartRequestPayload,
  createRequestId: () => string = () => crypto.randomUUID(),
): StartRequest {
  return existing ?? { requestId: createRequestId(), payload }
}

export function isUncertainStartFailure(error: unknown): boolean {
  return !(
    error instanceof ApiResponseError
    && error.status >= 400
    && error.status < 500
  )
}

export async function readResponse(response: Response) {
  let payload: unknown
  try {
    payload = await response.json()
  } catch {
    throw new Error('Backend returned an invalid JSON response.')
  }
  if (!response.ok) {
    const message = (
      payload
      && typeof payload === 'object'
      && 'error' in payload
      && typeof payload.error === 'string'
    ) ? payload.error : `Request failed (${response.status}).`
    throw new ApiResponseError(message, response.status)
  }
  return payload
}

export async function fetchWorkflowStatus(
  workflowId: string,
  fetcher: typeof fetch = fetch,
): Promise<WorkflowStatusResult> {
  try {
    const response = await fetcher(`/api/workflows/${workflowId}`, { cache: 'no-store' })
    const payload = await readResponse(response)
    if (
      !payload
      || typeof payload !== 'object'
      || !('workflow_id' in payload)
      || typeof payload.workflow_id !== 'string'
    ) {
      throw new Error('Backend returned an invalid workflow status response.')
    }
    return { ok: true, workflow: payload }
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : 'Unable to refresh workflow status.',
    }
  }
}

// Same rule as the backend's WORKFLOW_ID_PATTERN.
const WORKFLOW_ID = /^[a-f0-9]{32}(?:-retry-[1-2])?$/
const LAST_RUN_KEY = 'workflow-console:last-run'

// After a page refresh, reopen the run named in ?run=<id>, else the last run started here.
export function resumeWorkflowId(search: string, stored: string | null): string | null {
  const fromUrl = new URLSearchParams(search).get('run')
  for (const candidate of [fromUrl, stored]) {
    if (candidate && WORKFLOW_ID.test(candidate)) return candidate
  }
  return null
}

export function readLastRun(): string | null {
  try {
    return window.localStorage.getItem(LAST_RUN_KEY)
  } catch {
    return null
  }
}

export function rememberLastRun(workflowId: string | null) {
  try {
    if (workflowId) window.localStorage.setItem(LAST_RUN_KEY, workflowId)
    else window.localStorage.removeItem(LAST_RUN_KEY)
  } catch {
    // Storage can be blocked; resuming is only a convenience.
  }
}
