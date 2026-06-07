/**
 * WebSocket client for Foresight real-time subscriptions
 */
import { EventType, Event } from './types'

export interface WebSocketClientOptions {
  url: string
  userId?: string
  reconnectInterval?: number
  maxReconnectAttempts?: number
}

export interface SubscriptionOptions {
  eventTypes: EventType[]
  entityFilter?: string
  subscriptionId?: string
}

export type MessageHandler = (message: WebSocketMessage) => void
export type ConnectionState =
  | 'disconnected'
  | 'connecting'
  | 'connected'
  | 'reconnecting'

export interface WebSocketMessage {
  type:
    | 'connection_accepted'
    | 'subscribed'
    | 'unsubscribed'
    | 'event'
    | 'pong'
    | 'error'
  subscription_id?: string
  event_type?: string
  event_types?: string[]
  entity_filter?: string
  timestamp?: string
  payload?: Record<string, unknown>
  message?: string
}

export class ForesightWebSocketClient {
  private ws: WebSocket | null = null
  private readonly options: Required<WebSocketClientOptions>
  private state: ConnectionState = 'disconnected'
  private reconnectAttempts = 0
  private readonly messageHandlers: MessageHandler[] = []
  private readonly subscriptions: Map<string, SubscriptionOptions> = new Map()
  private connectionPromise: Promise<void> | null = null

  constructor(options: WebSocketClientOptions) {
    this.options = {
      url: options.url,
      userId: options.userId ?? 'default',
      reconnectInterval: options.reconnectInterval ?? 5000,
      maxReconnectAttempts: options.maxReconnectAttempts ?? 5,
    }
  }

  /**
   * Connect to the WebSocket server
   */
  connect(): Promise<void> {
    if (this.ws?.readyState === WebSocket.OPEN) {
      return Promise.resolve()
    }

    if (this.connectionPromise) {
      return this.connectionPromise
    }

    this.state = 'connecting'

    this.connectionPromise = new Promise((resolve, reject) => {
      try {
        this.ws = new WebSocket(this.options.url)

        this.ws.onopen = () => {
          this.state = 'connected'
          this.reconnectAttempts = 0
          this.reconnectSubscriptions()
          resolve()
        }

        this.ws.onclose = () => {
          this.state = 'disconnected'
          this.handleDisconnect()
        }

        this.ws.onerror = (error) => {
          this.state = 'disconnected'
          reject(error)
        }

        this.ws.onmessage = (event: MessageEvent) => {
          this.handleMessage(event.data as string)
        }
      } catch (error) {
        reject(error)
      }
    })

    return this.connectionPromise
  }

  /**
   * Disconnect from the WebSocket server
   */
  disconnect(): void {
    this.ws?.close()
    this.ws = null
    this.state = 'disconnected'
    this.connectionPromise = null
  }

  /**
   * Subscribe to events
   */
  async subscribe(options: SubscriptionOptions): Promise<string> {
    const subscriptionId = options.subscriptionId ?? this.generateId()

    this.subscriptions.set(subscriptionId, options)

    if (this.ws?.readyState === WebSocket.OPEN) {
      this.send({
        action: 'subscribe',
        subscription_id: subscriptionId,
        event_types: options.eventTypes,
        entity_filter: options.entityFilter,
      })
    }

    return subscriptionId
  }

  /**
   * Unsubscribe from events
   */
  async unsubscribe(subscriptionId: string): Promise<void> {
    this.subscriptions.delete(subscriptionId)

    if (this.ws?.readyState === WebSocket.OPEN) {
      this.send({
        action: 'unsubscribe',
        subscription_id: subscriptionId,
      })
    }
  }

  /**
   * Send ping to keep connection alive
   */
  ping(): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.send({ action: 'ping' })
    }
  }

  /**
   * Add message handler
   */
  onMessage(handler: MessageHandler): void {
    this.messageHandlers.push(handler)
  }

  /**
   * Remove message handler
   */
  offMessage(handler: MessageHandler): void {
    const index = this.messageHandlers.indexOf(handler)
    if (index !== -1) {
      this.messageHandlers.splice(index, 1)
    }
  }

  private send(message: Record<string, unknown>): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(message))
    }
  }

  private handleMessage(data: string): void {
    try {
      const message = JSON.parse(data) as WebSocketMessage
      this.messageHandlers.forEach((handler) => handler(message))
    } catch (error) {
      console.error('Failed to parse WebSocket message:', error)
    }
  }

  private handleDisconnect(): void {
    if (this.reconnectAttempts < this.options.maxReconnectAttempts) {
      this.reconnectAttempts++
      this.state = 'reconnecting'
      setTimeout(() => this.connect(), this.options.reconnectInterval)
    }
  }

  private reconnectSubscriptions(): void {
    for (const [id, options] of this.subscriptions.entries()) {
      this.send({
        action: 'subscribe',
        subscription_id: id,
        event_types: options.eventTypes,
        entity_filter: options.entityFilter,
      })
    }
  }

  private generateId(): string {
    return `sub_${Date.now()}_${Math.random().toString(36).substr(2, 9)}`
  }

  getState(): ConnectionState {
    return this.state
  }
}
