import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import {
  EVIDENCE_EXTENSIONS,
  evidenceFilesError,
  fetchWorkflowStatus,
  getStartRequest,
  isTerminalWorkflow,
  isUncertainStartFailure,
  readLastRun,
  readResponse,
  rememberLastRun,
  resumeWorkflowId,
  type StartRequest,
  type StartRequestPayload,
} from './workflowReliability'
import './WorkflowConsole.css'

type Stage = { stage: number; name: string; status: string }
type WorkflowReport = {
  final_status: string
  technology: string | null
  reproduction_source: string | null
  root_cause: string | null
  stages: Stage[]
  retry_history: Array<{ attempt: number; overall: string }>
  deployment_status: string
  deployment_readiness: string
  reproduction_confirmed?: boolean | null
  fix_review?: { decision: string | null; feedback: string | null; reviewed_by: string | null } | null
}
type EvidenceItem = { id: string; name: string; kind: string; bytes: number; redacted: boolean }
type ReproductionReview = {
  kind?: 'test'
  round: number
  max_rounds: number
  framework: string | null
  test_file: string | null
  classification: 'FAILS_ON_CURRENT_CODE' | 'PASSES_ON_CURRENT_CODE' | 'ERROR' | string
  content: string
  output_tail: string
  tests: number | null
  failed: number | null
}
type FixReview = {
  kind: 'fix'
  reason: string | null
  files: Array<{ file: string; added: number; removed: number }>
  diff: string
  diff_truncated: boolean
  checks: Record<string, string>
}
type RecordingReview = {
  kind: 'recording_result'
  filename: string | null
  classification: string
  summary: string
  steps: Array<{ line: number | null; source: string; status: string; message: string | null }>
}
type Workflow = {
  workflow_id: string
  status: string
  terminal: boolean
  stage: string
  blocker_reason: string | null
  pause_kind: string | null
  allowed_actions: string[]
  retry_count: number
  max_retries: number
  stages: Stage[]
  report: WorkflowReport | null
  review: ReproductionReview | FixReview | RecordingReview | null
  evidence: EvidenceItem[]
}
type ManualFields = {
  steps: string
  expected: string
  observed: string
  evidence: string
}

const emptyManual: ManualFields = { steps: '', expected: '', observed: '', evidence: '' }

const EVIDENCE_ACCEPT = EVIDENCE_EXTENSIONS.join(',')
const RECORDING_ACCEPT = '.js,.ts,.mjs,.cjs,.py'
const MAX_RECORDING_BYTES = 64 * 1024

function readAttachments(files: File[]) {
  return Promise.all(files.map(async (file) => ({ name: file.name, content: await file.text() })))
}

function formatBytes(bytes: number) {
  return bytes < 1024 ? `${bytes} B` : `${Math.round(bytes / 1024)} KB`
}

function lines(value: string) {
  return value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean)
}

function WorkflowConsole() {
  const [repositoryPath, setRepositoryPath] = useState('')
  const [bugTitle, setBugTitle] = useState('')
  const [bugDescription, setBugDescription] = useState('')
  const [manual, setManual] = useState(emptyManual)
  const [reviewFeedback, setReviewFeedback] = useState('')
  const [startFiles, setStartFiles] = useState<File[]>([])
  const [reviewFiles, setReviewFiles] = useState<File[]>([])
  const [recordingFile, setRecordingFile] = useState<File | null>(null)
  const [manualExpanded, setManualExpanded] = useState(false)
  const [allowLocalCommands, setAllowLocalCommands] = useState(false)
  const [allowAi, setAllowAi] = useState(false)
  const [workflow, setWorkflow] = useState<Workflow | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [startUncertain, setStartUncertain] = useState(false)
  const [pollError, setPollError] = useState('')
  const [pollAttempt, setPollAttempt] = useState(0)
  const startRequestRef = useRef<StartRequest | null>(null)

  const workflowTerminal = isTerminalWorkflow(workflow)

  // Reconnect to a run that is still going after the page is refreshed.
  useEffect(() => {
    const resumeId = resumeWorkflowId(window.location.search, readLastRun())
    if (!resumeId) return
    let active = true
    fetchWorkflowStatus(resumeId).then((result) => {
      if (!active) return
      if (result.ok) setWorkflow((current) => current ?? (result.workflow as Workflow))
      else rememberLastRun(null)
    })
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (workflow?.workflow_id) rememberLastRun(workflow.workflow_id)
  }, [workflow?.workflow_id])

  useEffect(() => {
    const workflowId = workflow?.workflow_id
    if (!workflowId || workflowTerminal) return
    let active = true
    let timer = 0
    const poll = async () => {
      const result = await fetchWorkflowStatus(workflowId)
      if (!active) return
      if (!result.ok) {
        setPollError(result.error)
        return
      }
      setPollError('')
      setWorkflow(result.workflow as Workflow)
      timer = window.setTimeout(poll, 1200)
    }
    timer = window.setTimeout(poll, 250)
    return () => {
      active = false
      window.clearTimeout(timer)
    }
  }, [workflow?.workflow_id, workflowTerminal, pollAttempt])

  const start = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    setPending(true)
    setError('')
    setWorkflow(null)
    const initialManual = manualExpanded && lines(manual.steps).length > 0
      ? {
          steps: lines(manual.steps),
          expected_behavior: manual.expected,
          observed_behavior: manual.observed,
          supporting_evidence: lines(manual.evidence),
        }
      : undefined
    let attachments: StartRequestPayload['attachments']
    if (startFiles.length > 0 && !startRequestRef.current) {
      const fileError = evidenceFilesError(startFiles)
      if (fileError) {
        setError(fileError)
        setPending(false)
        return
      }
      try {
        attachments = await readAttachments(startFiles)
      } catch {
        setError('The attached files could not be read. Choose them again.')
        setPending(false)
        return
      }
    }
    const payload: StartRequestPayload = {
      repository_path: repositoryPath,
      bug_title: bugTitle,
      bug_description: bugDescription,
      allow_local_commands: allowLocalCommands,
      allow_ai: allowAi,
      manual_reproduction: initialManual,
      attachments,
    }
    const startRequest = getStartRequest(startRequestRef.current, payload)
    startRequestRef.current = startRequest
    try {
      const response = await fetch('/api/workflows', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          request_id: startRequest.requestId,
          ...startRequest.payload,
        }),
      })
      const result = await readResponse(response)
      if (
        !result
        || typeof result !== 'object'
        || !('workflow_id' in result)
        || typeof result.workflow_id !== 'string'
      ) {
        throw new Error('Backend returned an invalid start response.')
      }
      startRequestRef.current = null
      setStartUncertain(false)
      setStartFiles([])
      setWorkflow(result as Workflow)
    } catch (startError) {
      const uncertain = isUncertainStartFailure(startError)
      setStartUncertain(uncertain)
      if (!uncertain) startRequestRef.current = null
      setError(startError instanceof Error ? startError.message : 'Unable to start workflow.')
    } finally {
      setPending(false)
    }
  }

  const submitManual = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!workflow) return
    setPending(true)
    setError('')
    try {
      const response = await fetch(`/api/workflows/${workflow.workflow_id}/manual-reproduction`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          submission_id: crypto.randomUUID(),
          manual_reproduction: {
            steps: lines(manual.steps),
            expected_behavior: manual.expected,
            observed_behavior: manual.observed,
            supporting_evidence: lines(manual.evidence),
          },
        }),
      })
      const payload = await readResponse(response)
      setWorkflow(payload as Workflow)
    } catch (submitError) {
      setError(submitError instanceof Error ? submitError.message : 'Unable to submit reproduction details.')
    } finally {
      setPending(false)
    }
  }

  const submitRecording = async () => {
    if (!workflow || !recordingFile) return
    if (!/^[\w.-]{1,100}\.(js|ts|mjs|cjs|py)$/.test(recordingFile.name)) {
      setError('Choose a codegen script ending in .js, .ts, .mjs, .cjs or .py (letters, digits, . _ - in the name).')
      return
    }
    if (recordingFile.size > MAX_RECORDING_BYTES) {
      setError('The recording is larger than 64 KB.')
      return
    }
    setPending(true)
    setError('')
    try {
      const response = await fetch(`/api/workflows/${workflow.workflow_id}/recording`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          submission_id: crypto.randomUUID(),
          recording: { filename: recordingFile.name, content: await recordingFile.text() },
        }),
      })
      const payload = await readResponse(response)
      setWorkflow(payload as Workflow)
      setRecordingFile(null)
    } catch (recordingError) {
      setError(recordingError instanceof Error ? recordingError.message : 'Unable to upload the recording.')
    } finally {
      setPending(false)
    }
  }

  const sendAction = async (action: 'retry' | 'continue' | 'stop_safely') => {
    if (!workflow) return
    setPending(true)
    setError('')
    try {
      const response = await fetch(`/api/workflows/${workflow.workflow_id}/actions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action_id: crypto.randomUUID(), action }),
      })
      const payload = await readResponse(response)
      setWorkflow(payload as Workflow)
    } catch (actionError) {
      setError(actionError instanceof Error ? actionError.message : 'Unable to submit workflow action.')
    } finally {
      setPending(false)
    }
  }

  const uploadEvidence = async () => {
    if (!workflow) return
    const fileError = evidenceFilesError(reviewFiles, workflow.evidence ?? [])
    if (fileError) {
      setError(fileError)
      return
    }
    setPending(true)
    setError('')
    try {
      const attachments = await readAttachments(reviewFiles)
      const response = await fetch(`/api/workflows/${workflow.workflow_id}/evidence`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ upload_id: crypto.randomUUID(), attachments }),
      })
      const payload = await readResponse(response)
      setWorkflow(payload as Workflow)
      setReviewFiles([])
    } catch (uploadError) {
      setError(uploadError instanceof Error ? uploadError.message : 'Unable to attach the files.')
    } finally {
      setPending(false)
    }
  }

  const sendReview = async (decision: 'confirm' | 'revise' | 'manual') => {
    if (!workflow) return
    setPending(true)
    setError('')
    try {
      const response = await fetch(`/api/workflows/${workflow.workflow_id}/reproduction-review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          review_id: crypto.randomUUID(),
          decision,
          feedback: decision === 'revise' ? reviewFeedback : '',
        }),
      })
      const payload = await readResponse(response)
      setWorkflow(payload as Workflow)
      setReviewFeedback('')
    } catch (reviewError) {
      setError(reviewError instanceof Error ? reviewError.message : 'Unable to submit the review.')
    } finally {
      setPending(false)
    }
  }

  const terminal = workflowTerminal
  const waitingForManual = !terminal && workflow?.pause_kind === 'manual_reproduction'
  const waitingForReview = !terminal && workflow?.pause_kind === 'reproduction_test_review' && Boolean(workflow?.review)
  const waitingForFixReview = !terminal && workflow?.pause_kind === 'fix_review' && workflow?.review?.kind === 'fix'

  const sendFixReview = async (decision: 'approve' | 'reject') => {
    if (!workflow) return
    setPending(true)
    setError('')
    try {
      const response = await fetch(`/api/workflows/${workflow.workflow_id}/fix-review`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ review_id: crypto.randomUUID(), decision, feedback: decision === 'reject' ? reviewFeedback : '' }),
      })
      const payload = await readResponse(response)
      setWorkflow(payload as Workflow)
      setReviewFeedback('')
    } catch (reviewError) {
      setError(reviewError instanceof Error ? reviewError.message : 'Unable to submit the fix review.')
    } finally {
      setPending(false)
    }
  }
  const retryStatusPolling = () => {
    setPollError('')
    setPollAttempt((attempt) => attempt + 1)
  }

  return (
    <main className="workflow-shell">
      <header className="workflow-topbar">
        <a className="product-name" href="/" aria-label="Fieldwork workflow console"><span className="product-monogram">FW</span><span>Fieldwork<small>DEBUGGING WORKFLOW</small></span></a>
        <div className="local-indicator"><i /> LOCAL CONTROL PLANE</div>
      </header>

      <section className="workflow-heading">
        <div><p className="workflow-kicker">HUMAN-IN-THE-LOOP / 01</p><h1>Debug with a human in the loop.</h1><p>Submit a bug, monitor each stage, and resolve blockers before the workflow continues.</p></div>
        <nav className="workflow-tabs" aria-label="Workspace">
          <span className="selected-tab" aria-current="page">Workflow console</span>
          <a href="/websocket-demo">Level 3 WebSocket demo ↗</a>
        </nav>
      </section>

      <div className="workflow-columns">
        <section className="request-panel" aria-labelledby="request-title">
          <div className="panel-heading"><div><span className="workflow-kicker">RUN REQUEST</span><h2 id="request-title">Bug report</h2></div><span className="panel-index">01 <i>/</i> 02</span></div>

          {!workflow && (
            <form className="workflow-form" onSubmit={start}>
              <label htmlFor="repository-path">Repository or working-copy path</label>
              <input id="repository-path" value={repositoryPath} onChange={(event) => setRepositoryPath(event.target.value)} placeholder="C:\\Projects\\my-application" maxLength={4096} required disabled={pending || startUncertain} />
              <label htmlFor="bug-title">Bug title</label>
              <input id="bug-title" value={bugTitle} onChange={(event) => setBugTitle(event.target.value)} placeholder="Messages overwrite earlier records" maxLength={180} required disabled={pending || startUncertain} />
              <label htmlFor="bug-description">Bug description</label>
              <textarea id="bug-description" value={bugDescription} onChange={(event) => setBugDescription(event.target.value)} placeholder="Describe the failure and where it occurs." maxLength={8000} rows={4} required disabled={pending || startUncertain} />

              <button className="expand-manual" type="button" aria-expanded={manualExpanded} onClick={() => setManualExpanded(!manualExpanded)} disabled={pending || startUncertain}><span>{manualExpanded ? '−' : '+'}</span> Add reproduction details <small>Optional</small></button>
              {manualExpanded && <ManualEvidence value={manual} onChange={setManual} disabled={pending || startUncertain} />}

              <EvidencePicker id="start-evidence" files={startFiles} onChange={setStartFiles} disabled={pending || startUncertain} />

              <div className="approval-list">
                <label className="approval-item"><input type="checkbox" checked={allowLocalCommands} onChange={(event) => setAllowLocalCommands(event.target.checked)} disabled={pending || startUncertain} /><span><strong>Allow local test and build commands</strong><small>Commands are selected by the existing workflow, not entered here.</small></span></label>
                <label className="approval-item"><input type="checkbox" checked={allowAi} onChange={(event) => setAllowAi(event.target.checked)} disabled={pending || startUncertain} /><span><strong>Allow AI model calls</strong><small>Diagnosis, fixing, and test generation may contact the configured provider.</small></span></label>
              </div>
              {error && <p className="workflow-error" role="alert">{error}</p>}
              {startUncertain && <p className="workflow-error" role="alert">The start result is unconfirmed. Retrying will resend the same request ID and report.</p>}
              <button className="workflow-primary" type="submit" disabled={pending || (!startUncertain && (!allowLocalCommands || !allowAi))}>{pending ? 'Starting…' : startUncertain ? 'Retry start request' : 'Start workflow'} <span aria-hidden="true">↗</span></button>
              <p className="form-footnote">Paths must be inside the workspace or configured allowed roots. Deployment is not available from this interface.</p>
            </form>
          )}

          {workflow && (
            <div className="active-workflow">
              <span className="workflow-kicker">WORKFLOW ID</span><code>{workflow.workflow_id}</code>
              {(workflow.evidence ?? []).length > 0 && <div className="evidence-list"><span className="workflow-kicker">ATTACHED EVIDENCE</span><ul>{workflow.evidence.map((item) => <li key={item.id}><span>{item.name}</span><small>{item.kind.replace('_', ' ')} · {formatBytes(item.bytes)}{item.redacted ? ' · secrets removed' : ''}</small></li>)}</ul></div>}
              <div className="run-controls">
                <button className="subtle-button" type="button" disabled={!terminal} onClick={() => { setWorkflow(null); setError(''); rememberLastRun(null) }}>Start another run</button>
                {!terminal && workflow.status !== 'BLOCKED' && <button className="subtle-button stop-control" type="button" disabled={pending || workflow.status === 'STOPPING'} onClick={() => sendAction('stop_safely')}>{workflow.status === 'STOPPING' ? 'Stopping at the next safe checkpoint…' : 'Stop safely'}</button>}
              </div>

              {waitingForManual && workflow.allowed_actions.includes('upload_recording') && (
                <RecordingPanel
                  file={recordingFile}
                  onFileChange={setRecordingFile}
                  onSubmit={submitRecording}
                  pending={pending}
                  lastResult={workflow.review?.kind === 'recording_result' ? workflow.review as RecordingReview : null}
                />
              )}

              {waitingForManual && (
                <form className="human-pause" onSubmit={submitManual}>
                  <span className="workflow-kicker">INPUT REQUIRED</span><h3>Automatic reproduction was inconclusive</h3>
                  <p>Your evidence will be stored in the existing manual reproduction artifact and included in initial and retry diagnosis.</p>
                  <ManualEvidence value={manual} onChange={setManual} />
                  {error && <p className="workflow-error" role="alert">{error}</p>}
                  <button className="workflow-primary" type="submit" disabled={pending}>Submit reproduction evidence</button>
                </form>
              )}

              {waitingForFixReview && workflow.review?.kind === 'fix' && (
                <FixReviewPanel review={workflow.review} feedback={reviewFeedback} onFeedbackChange={setReviewFeedback} onDecision={sendFixReview} pending={pending} error={error} />
              )}

              {waitingForReview && workflow.review && workflow.review.kind !== 'fix' && (
                <ReviewPanel
                  review={workflow.review as ReproductionReview}
                  allowedActions={workflow.allowed_actions}
                  feedback={reviewFeedback}
                  onFeedbackChange={setReviewFeedback}
                  onDecision={sendReview}
                  pending={pending}
                  error={error}
                  attachSlot={<div className="review-attach">
                    <EvidencePicker id="review-evidence" files={reviewFiles} onChange={setReviewFiles} disabled={pending} label="Add recordings or logs for the next revision" />
                    {reviewFiles.length > 0 && <button className="workflow-secondary" type="button" disabled={pending} onClick={uploadEvidence}>Attach {reviewFiles.length} file{reviewFiles.length === 1 ? '' : 's'}</button>}
                  </div>}
                />
              )}

              {!terminal && workflow.status === 'BLOCKED' && workflow.allowed_actions.length > 0 && (
                <div className="blocker-panel"><span className="workflow-kicker">ACTION REQUIRED</span><p>{workflow.blocker_reason}</p><div className="blocker-actions">
                  {workflow.allowed_actions.includes('retry') && <button className="workflow-secondary" type="button" disabled={pending} onClick={() => sendAction('retry')}>Retry · {workflow.retry_count}/{workflow.max_retries}</button>}
                  {workflow.allowed_actions.includes('continue') && <button className="workflow-secondary" type="button" disabled={pending} onClick={() => sendAction('continue')}>Continue</button>}
                  {workflow.allowed_actions.includes('stop_safely') && <button className="workflow-danger" type="button" disabled={pending} onClick={() => sendAction('stop_safely')}>Stop safely</button>}
                </div></div>
              )}
            </div>
          )}
        </section>

        <section className="monitor-panel" aria-labelledby="monitor-title">
          <div className="panel-heading"><div><span className="workflow-kicker">LIVE RUN</span><h2 id="monitor-title">Workflow monitor</h2></div><span className={`workflow-status ${workflow?.status.toLowerCase().replaceAll('_', '-') ?? 'idle'}`}><i />{workflow?.status ?? 'IDLE'}</span></div>
          {!workflow ? <div className="monitor-empty"><span aria-hidden="true">↳</span><p>Stage progress, blockers, and the final report appear here.</p></div> : <>
            <div className="current-stage"><span className="workflow-kicker">CURRENT STAGE</span><strong>{workflow.stage}</strong><p>{stageMessage(workflow, terminal)}</p></div>
            {pollError && <div className="workflow-error" role="alert"><p>Workflow status connection failed: {pollError}</p><button className="subtle-button" type="button" onClick={retryStatusPolling}>Retry status polling</button></div>}
            <StageList stages={workflow.stages} status={workflow.status} terminal={terminal} />
            {workflow.report && <FinalReport report={workflow.report} />}
          </>}
        </section>
      </div>
      <footer className="workflow-footer"><span>Run artifacts remain local.</span><span>NOT DEPLOYED · READINESS NOT ASSESSED</span></footer>
    </main>
  )
}

function ManualEvidence({ value, onChange, disabled = false }: { value: ManualFields; onChange: (value: ManualFields) => void; disabled?: boolean }) {
  const update = (key: keyof ManualFields, next: string) => onChange({ ...value, [key]: next })
  return <div className="manual-evidence">
    <label htmlFor="manual-steps">Reproduction steps <small>One per line</small></label>
    <textarea id="manual-steps" value={value.steps} onChange={(event) => update('steps', event.target.value)} rows={3} maxLength={12000} placeholder={'Open the page\nSubmit two messages'} disabled={disabled} />
    <label htmlFor="manual-expected">Expected behavior</label>
    <textarea id="manual-expected" value={value.expected} onChange={(event) => update('expected', event.target.value)} rows={2} maxLength={4000} disabled={disabled} />
    <label htmlFor="manual-observed">Actual behavior</label>
    <textarea id="manual-observed" value={value.observed} onChange={(event) => update('observed', event.target.value)} rows={2} maxLength={4000} disabled={disabled} />
    <label htmlFor="manual-evidence">Evidence <small>Optional, one item per line</small></label>
    <textarea id="manual-evidence" value={value.evidence} onChange={(event) => update('evidence', event.target.value)} rows={2} maxLength={12000} placeholder="Error output, test results, or observed records" disabled={disabled} />
  </div>
}

const REVIEW_RESULT: Record<string, { label: string; tone: string }> = {
  FAILS_ON_CURRENT_CODE: { label: 'Fails on the current code', tone: 'fails' },
  PASSES_ON_CURRENT_CODE: { label: 'Passes on the current code: does not reproduce the bug', tone: 'passes' },
  ERROR: { label: 'Could not run correctly', tone: 'error' },
}

function EvidencePicker({ id, files, onChange, disabled, label = 'Recordings or logs' }: {
  id: string
  files: File[]
  onChange: (files: File[]) => void
  disabled: boolean
  label?: string
}) {
  return <div className="evidence-picker">
    <label htmlFor={id}>{label} <small>Optional</small></label>
    <input id={id} type="file" multiple accept={EVIDENCE_ACCEPT} disabled={disabled} onChange={(event) => onChange(Array.from(event.target.files ?? []))} />
    <small className="evidence-hint">HAR browser recordings, API or WebSocket message logs (.json), error logs (.log, .txt) or recorded test scripts. Up to 5 files, 256 KB each. Passwords, tokens and cookies are removed before saving; files are never run.</small>
    {files.length > 0 && <ul className="evidence-chosen">{files.map((file) => <li key={`${file.name}-${file.size}`}>{file.name} <small>{formatBytes(file.size)}</small></li>)}</ul>}
  </div>
}

const RECORDING_RESULT: Record<string, { label: string; tone: string }> = {
  FAILS_ON_CURRENT_CODE: { label: 'Bug shown: a recorded assertion failed', tone: 'fails' },
  PASSES_ON_CURRENT_CODE: { label: 'Everything passed: the recording does not show the bug', tone: 'passes' },
  ERROR: { label: 'A step could not run', tone: 'error' },
  UNAVAILABLE: { label: 'Playwright is not installed', tone: 'error' },
  REJECTED: { label: 'Recording rejected', tone: 'error' },
}

function RecordingPanel({ file, onFileChange, onSubmit, pending, lastResult }: {
  file: File | null
  onFileChange: (file: File | null) => void
  onSubmit: () => void
  pending: boolean
  lastResult: RecordingReview | null
}) {
  const result = lastResult ? (RECORDING_RESULT[lastResult.classification] ?? RECORDING_RESULT.ERROR) : null
  return <div className="human-pause recording-panel">
    <span className="workflow-kicker">OPTION A · RECORD THE BUG</span>
    <h3>Upload a Playwright recording</h3>
    <p>Record the bug with <code>npx playwright codegen &lt;app URL&gt;</code>, and use the toolbar to assert what the correct app should show. The workflow starts the app itself, replays the recorded steps (never the script), and accepts the recording only if one of your assertions fails. After the fix, every step must pass.</p>
    <small className="evidence-hint">Stop your own copy of the app before uploading, so the workflow can use its port. Only local pages, and only clicks, typing, key presses, selections and assertions are allowed.</small>
    {lastResult && result && <div className="recording-result">
      <div className={`review-result ${result.tone}`}><strong>{result.label}</strong>{lastResult.filename && <span>{lastResult.filename}</span>}</div>
      <p>{lastResult.summary}</p>
      {lastResult.steps.length > 0 && <ol className="recording-steps">{lastResult.steps.map((step, index) => <li key={`${step.line}-${index}`} className={step.status}>
        <span className="recording-step-status">{step.status === 'passed' ? '✓' : step.status === 'failed' ? '✗' : '!'}</span>
        <code>{step.source}</code>{step.message && <small>{step.message}</small>}
      </li>)}</ol>}
    </div>}
    <label htmlFor="recording-file">Codegen script <small>.js, .ts or .py · up to 64 KB</small></label>
    <input id="recording-file" type="file" accept={RECORDING_ACCEPT} disabled={pending} onChange={(event) => onFileChange(event.target.files?.[0] ?? null)} />
    <div className="blocker-actions">
      <button className="workflow-primary" type="button" disabled={pending || !file} onClick={onSubmit}>Replay recording on the current code</button>
    </div>
    <span className="workflow-kicker recording-or">OR · OPTION B · DESCRIBE THE BUG BELOW</span>
  </div>
}

function ReviewPanel({ review, allowedActions, feedback, onFeedbackChange, onDecision, pending, error, attachSlot }: {
  review: ReproductionReview
  allowedActions: string[]
  feedback: string
  onFeedbackChange: (value: string) => void
  onDecision: (decision: 'confirm' | 'revise' | 'manual') => void
  pending: boolean
  error: string
  attachSlot?: ReactNode
}) {
  const result = REVIEW_RESULT[review.classification] ?? REVIEW_RESULT.ERROR
  const canConfirm = allowedActions.includes('confirm')
  return <div className="human-pause review-panel">
    <span className="workflow-kicker">REVIEW REQUIRED · ROUND {review.round} OF {review.max_rounds}</span>
    <h3>Does this test reproduce the bug?</h3>
    <p>The AI wrote this test and ran it on the current code. Confirm it only if it fails because of the reported bug. After the fix, the same test must pass.</p>
    <div className={`review-result ${result.tone}`}><strong>{result.label}</strong>{review.tests !== null && <span>{review.failed ?? 0} of {review.tests} test{review.tests === 1 ? '' : 's'} failed</span>}</div>
    <span className="review-file">{review.test_file} · {review.framework}</span>
    <pre className="review-code" tabIndex={0} aria-label="Reproduction test code"><code>{review.content}</code></pre>
    {review.output_tail && <details className="review-output"><summary>Test output</summary><pre tabIndex={0}><code>{review.output_tail}</code></pre></details>}
    <label htmlFor="review-feedback">What should the test do differently? <small>Needed for a revision</small></label>
    <textarea id="review-feedback" value={feedback} onChange={(event) => onFeedbackChange(event.target.value)} rows={3} maxLength={4000} placeholder="For example: send both messages on the same connection, then check both are stored." disabled={pending} />
    {attachSlot}
    {error && <p className="workflow-error" role="alert">{error}</p>}
    <div className="blocker-actions">
      {canConfirm && <button className="workflow-primary" type="button" disabled={pending} onClick={() => onDecision('confirm')}>Confirm: this is the bug</button>}
      <button className="workflow-secondary" type="button" disabled={pending || !feedback.trim()} onClick={() => onDecision('revise')}>Revise the test</button>
      <button className="workflow-secondary" type="button" disabled={pending} onClick={() => onDecision('manual')}>Describe the bug manually instead</button>
    </div>
  </div>
}

function FixReviewPanel({ review, feedback, onFeedbackChange, onDecision, pending, error }: {
  review: FixReview
  feedback: string
  onFeedbackChange: (value: string) => void
  onDecision: (decision: 'approve' | 'reject') => void
  pending: boolean
  error: string
}) {
  return <div className="human-pause review-panel">
    <span className="workflow-kicker">APPROVAL REQUIRED</span>
    <h3>Approve this fix?</h3>
    <p>{review.reason ?? 'The bug was not confirmed by an automated test.'} Check that the changes fix the reported bug.</p>
    <dl className="fix-checks">{Object.entries(review.checks ?? {}).map(([name, value]) => <div key={name}><dt>{name.replace('_', ' ')}</dt><dd className={value === 'PASS' ? 'ok' : 'bad'}>{value}</dd></div>)}</dl>
    <ul className="fix-files">{review.files.map((file) => <li key={file.file}><span>{file.file}</span><small>+{file.added} −{file.removed}</small></li>)}</ul>
    <pre className="review-code" tabIndex={0} aria-label="Changes made by the fix"><code>{review.diff || 'No file changes were recorded.'}</code></pre>
    {review.diff_truncated && <small className="evidence-hint">The diff was shortened; the full change is in the run folder.</small>}
    <label htmlFor="fix-review-feedback">Reason for rejecting <small>Needed to reject</small></label>
    <textarea id="fix-review-feedback" value={feedback} onChange={(event) => onFeedbackChange(event.target.value)} rows={2} maxLength={4000} disabled={pending} placeholder="For example: the change does not affect the reported handler." />
    {error && <p className="workflow-error" role="alert">{error}</p>}
    <div className="blocker-actions">
      <button className="workflow-primary" type="button" disabled={pending} onClick={() => onDecision('approve')}>Approve fix</button>
      <button className="workflow-danger" type="button" disabled={pending || !feedback.trim()} onClick={() => onDecision('reject')}>Reject fix</button>
    </div>
  </div>
}

function stageMessage(workflow: Workflow, terminal: boolean) {
  if (workflow.blocker_reason) return workflow.blocker_reason
  if (terminal) {
    if (workflow.status === 'PASS') return 'The run passed. Tests, builds and fix verification succeeded. See the final report below.'
    if (workflow.status === 'STOPPED') return 'The run was stopped safely. Completed stages and artifacts were kept. See the final report below.'
    if (workflow.status === 'BLOCKED') return 'The run ended blocked. See the final report below for what was missing.'
    if (workflow.status === 'UNCONFIRMED') return 'Checks passed, but the bug was never confirmed by an automated test and nobody approved the fix, so it is not marked as passed.'
    return 'The run finished without a verified fix. See the final report below for the reason.'
  }
  if (workflow.status === 'STOPPING') return 'Stopping at the next safe checkpoint. The current stage will finish first.'
  if (workflow.status === 'SUBMISSION_RECEIVED') return 'Your reproduction evidence was received. The workflow will continue shortly.'
  if (workflow.status === 'RECORDING_SUBMITTED' || workflow.status === 'REPLAYING_RECORDING') return 'Replaying your recording: the workflow is starting the app and running the recorded steps on the current code. This can take a minute.'
  if (workflow.status === 'REVIEW_SUBMITTED') return 'Your review was received. The workflow will continue shortly.'
  if (workflow.status === 'ACTION_SUBMITTED') return 'Your action was received. The workflow will continue shortly.'
  if (workflow.status === 'QUEUED') return 'Starting the workflow.'
  return 'Running. This view updates automatically.'
}

function StageList({ stages, status, terminal }: { stages: Stage[]; status: string; terminal: boolean }) {
  const visibleStages = stages.length > 0 ? stages : [{ stage: 0, name: 'Waiting for the first stage to start', status: 'QUEUED' }]
  return <ol className="stage-list" aria-label="Workflow stages">
    {visibleStages.map((stage, index) => <li className={`stage-row ${stage.status.toLowerCase()}`} key={`${stage.stage}-${stage.name}`}>
      <span className="stage-index">{stage.status === 'SUCCESS' ? '✓' : String(index + 1).padStart(2, '0')}</span><span className="stage-label">{stage.name}</span><span className="stage-result">{stage.status}</span>
    </li>)}
    {!terminal && stages.length > 0 && !stages.some((stage) => stage.status === 'RUNNING') && <li className="stage-row stage-active"><span className="stage-index">•</span><span className="stage-label">{status}</span><span className="stage-result">ACTIVE</span></li>}
  </ol>
}

const REPRODUCTION_LABELS: Record<string, string> = {
  automatic: 'Confirmed automatically',
  ai_reproduction_test: 'Confirmed by a reviewed test',
  browser_recording: 'Confirmed by a replayed browser recording',
  manual: 'User-reported only (not confirmed by a test)',
}

function FinalReport({ report }: { report: WorkflowReport }) {
  const reproduction = report.reproduction_source ? (REPRODUCTION_LABELS[report.reproduction_source] ?? report.reproduction_source) : 'Not confirmed'
  const review = report.fix_review
  return <div className="final-report"><span className="workflow-kicker">FINAL REPORT</span>
    <dl><div><dt>Technology</dt><dd>{report.technology ?? 'Not detected'}</dd></div><div><dt>Reproduction</dt><dd>{reproduction}</dd></div>{review && <div><dt>Fix review</dt><dd>{review.decision === 'approve' ? 'Approved by user' : review.decision === 'reject' ? `Rejected: ${review.feedback ?? ''}` : 'Not reviewed'}</dd></div>}<div><dt>Deployment</dt><dd>{report.deployment_status} · {report.deployment_readiness}</dd></div></dl>
    {report.root_cause && <p className="root-cause"><strong>Root cause</strong>{report.root_cause}</p>}
    {report.retry_history.length > 0 && <p className="retry-summary">{report.retry_history.length} retry attempt{report.retry_history.length === 1 ? '' : 's'} recorded</p>}
  </div>
}

export default WorkflowConsole