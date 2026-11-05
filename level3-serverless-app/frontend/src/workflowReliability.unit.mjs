import assert from 'node:assert/strict'
import test from 'node:test'

import {
  ApiResponseError,
  evidenceFilesError,
  fetchWorkflowStatus,
  getStartRequest,
  isTerminalWorkflow,
  isUncertainStartFailure,
  resumeWorkflowId,
} from './workflowReliability.ts'

test('polling continues until the backend marks the workflow terminal', () => {
  assert.equal(isTerminalWorkflow(null), false)
  assert.equal(isTerminalWorkflow({ status: 'RUNNING', terminal: false }), false)
  // A report may exist before the final status lands; keep polling.
  assert.equal(isTerminalWorkflow({ status: 'RUNNING', terminal: false, report: {} }), false)
  assert.equal(isTerminalWorkflow({ status: 'BLOCKED', terminal: false }), false)
  assert.equal(isTerminalWorkflow({ status: 'BLOCKED', terminal: true }), true)
  assert.equal(isTerminalWorkflow({ status: 'STOPPED' }), true)
  assert.equal(isTerminalWorkflow({ status: 'STOPPING' }), false)
})

test('a start retry reuses its request ID and original payload', () => {
  const payload = { bug_title: 'Original report' }
  const first = getStartRequest(null, payload, () => 'stable-request-id')
  const retry = getStartRequest(first, { bug_title: 'Edited report' }, () => 'new-request-id')

  assert.equal(retry.requestId, 'stable-request-id')
  assert.equal(retry.payload, payload)
})

test('only explicit client errors confirm that a start request was rejected', () => {
  assert.equal(isUncertainStartFailure(new TypeError('Network unavailable')), true)
  assert.equal(isUncertainStartFailure(new ApiResponseError('Server error', 503)), true)
  assert.equal(isUncertainStartFailure(new ApiResponseError('Invalid request', 400)), false)
})

test('status polling reports a connection failure and can be retried', async () => {
  let calls = 0
  const fetcher = async () => {
    calls += 1
    if (calls === 1) throw new TypeError('Network unavailable')
    return new Response(JSON.stringify({ workflow_id: 'workflow-1', status: 'RUNNING' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })
  }

  const failed = await fetchWorkflowStatus('workflow-1', fetcher)
  assert.deepEqual(failed, { ok: false, error: 'Network unavailable' })

  const retried = await fetchWorkflowStatus('workflow-1', fetcher)
  assert.equal(retried.ok, true)
  if (retried.ok) assert.equal(retried.workflow.workflow_id, 'workflow-1')
  assert.equal(calls, 2)
})

test('evidence files follow the backend limits', () => {
  assert.equal(evidenceFilesError([{ name: 'session.har', size: 1000 }]), null)
  assert.match(evidenceFilesError([{ name: 'trace.zip', size: 10 }]), /attach \.har/)
  assert.match(evidenceFilesError([{ name: 'big.log', size: 300 * 1024 }]), /256 KB/)
  assert.match(evidenceFilesError([{ name: 'a.log', size: 1 }], Array(5).fill({ bytes: 1 })), /At most 5/)
  assert.match(evidenceFilesError([{ name: 'empty.txt', size: 0 }]), /empty/)
  assert.match(evidenceFilesError([]), /at least one/)
})

test('a refreshed page resumes a valid run ID from the URL first, then storage', () => {
  const id = 'f60d222723a04469ae055e9a3cd8b96d'
  const other = 'a'.repeat(32)
  assert.equal(resumeWorkflowId(`?run=${id}`, other), id)
  assert.equal(resumeWorkflowId('', other), other)
  assert.equal(resumeWorkflowId('?run=../../etc', null), null)
  assert.equal(resumeWorkflowId('?run=bad', other), other)
  assert.equal(resumeWorkflowId('', null), null)
})
