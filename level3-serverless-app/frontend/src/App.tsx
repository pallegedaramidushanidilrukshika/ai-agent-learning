import { useEffect, useRef, useState } from 'react'
import './App.css'

const WEBSOCKET_URL =
  'wss://34hq0prlml.execute-api.ap-southeast-2.amazonaws.com/dev'
const ACKNOWLEDGEMENT_TIMEOUT_MS = 10_000

type DeliveryStatus = 'Idle' | 'Pending' | 'Saved' | 'Failed' | 'Not confirmed'

function App() {
  const [message, setMessage] = useState('')
  const [connectionStatus, setConnectionStatus] = useState('Connecting...')
  const [deliveryStatus, setDeliveryStatus] = useState<DeliveryStatus>('Idle')
  const [deliveryError, setDeliveryError] = useState('')
  const socketRef = useRef<WebSocket | null>(null)
  const currentRequestIdRef = useRef<string | null>(null)
  const acknowledgementTimeoutRef = useRef<number | null>(null)

  useEffect(() => {
    const socket = new WebSocket(WEBSOCKET_URL)

    socketRef.current = socket

    const markPendingNotConfirmed = (reason: string) => {
      if (!currentRequestIdRef.current) {
        return
      }

      currentRequestIdRef.current = null
      if (acknowledgementTimeoutRef.current !== null) {
        window.clearTimeout(acknowledgementTimeoutRef.current)
        acknowledgementTimeoutRef.current = null
      }
      setDeliveryStatus('Not confirmed')
      setDeliveryError(reason)
    }

    socket.onopen = () => {
      console.log('WebSocket connected')
      setConnectionStatus('Connected')
    }

    socket.onmessage = (event) => {
      console.log('Message from server:', event.data)

      let acknowledgement: {
        type?: string
        requestId?: string
        status?: string
        error?: string
      } | null
      try {
        acknowledgement = JSON.parse(event.data)
      } catch {
        return
      }

      if (
        !acknowledgement ||
        typeof acknowledgement !== 'object' ||
        acknowledgement.type !== 'message_ack' ||
        acknowledgement.requestId !== currentRequestIdRef.current
      ) {
        return
      }

      currentRequestIdRef.current = null
      if (acknowledgementTimeoutRef.current !== null) {
        window.clearTimeout(acknowledgementTimeoutRef.current)
        acknowledgementTimeoutRef.current = null
      }

      if (acknowledgement.status === 'saved') {
        setDeliveryStatus('Saved')
        setDeliveryError('')
        setMessage('')
      } else if (acknowledgement.status === 'failed') {
        setDeliveryStatus('Failed')
        setDeliveryError(acknowledgement.error || 'The message was not saved.')
      } else {
        setDeliveryStatus('Not confirmed')
        setDeliveryError(
          'The acknowledgement was not recognized. The message may have been saved.',
        )
      }
    }

    socket.onerror = (error) => {
      console.error('WebSocket error:', error)
      setConnectionStatus('Connection Error')
      markPendingNotConfirmed('Connection lost before the save was confirmed.')
    }

    socket.onclose = () => {
      console.log('WebSocket disconnected')
      setConnectionStatus('Disconnected')
      markPendingNotConfirmed('Connection closed before the save was confirmed.')
    }

    return () => {
      if (acknowledgementTimeoutRef.current !== null) {
        window.clearTimeout(acknowledgementTimeoutRef.current)
      }
      currentRequestIdRef.current = null
      socket.close()
    }
  }, [])

  const handleSend = () => {
    if (!socketRef.current || socketRef.current.readyState !== WebSocket.OPEN) {
      console.log('WebSocket is not connected')
      return
    }

    if (message.trim() === '') {
      setDeliveryStatus('Failed')
      setDeliveryError('Enter a message containing non-whitespace text.')
      return
    }

    const requestId = crypto.randomUUID()
    const payload = {
      action: 'message',
      requestId,
      message: message,
    }

    currentRequestIdRef.current = requestId
    setDeliveryStatus('Pending')
    setDeliveryError('')
    acknowledgementTimeoutRef.current = window.setTimeout(() => {
      if (currentRequestIdRef.current === requestId) {
        acknowledgementTimeoutRef.current = null
        setDeliveryStatus('Not confirmed')
        setDeliveryError(
          'No save confirmation arrived. The message may have been saved.',
        )
      }
    }, ACKNOWLEDGEMENT_TIMEOUT_MS)

    try {
      socketRef.current.send(JSON.stringify(payload))
    } catch (error) {
      currentRequestIdRef.current = null
      if (acknowledgementTimeoutRef.current !== null) {
        window.clearTimeout(acknowledgementTimeoutRef.current)
        acknowledgementTimeoutRef.current = null
      }
      setDeliveryStatus('Failed')
      setDeliveryError(
        error instanceof Error ? error.message : 'Unable to send the message.',
      )
      return
    }

    console.log('Message sent:', payload)
  }

  return (
    <div className="app">
      <h1>Level 3 WebSocket App</h1>

      <p className="status">
        Connection: {connectionStatus}
      </p>
      <p aria-live="polite">Message: {deliveryStatus}</p>
      {deliveryError && <p role="alert">{deliveryError}</p>}

      <div className="message-section">
        <label htmlFor="message">Message</label>

        <input
          id="message"
          type="text"
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          placeholder="Enter a message"
          disabled={deliveryStatus === 'Pending'}
        />

        <button
          type="button"
          onClick={handleSend}
          disabled={connectionStatus !== 'Connected' || deliveryStatus === 'Pending'}
        >
          {deliveryStatus === 'Pending' ? 'Sending…' : 'Send'}
        </button>
      </div>
    </div>
  )
}

export default App