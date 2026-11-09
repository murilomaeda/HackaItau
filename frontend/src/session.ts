// Sessão: o backend confere usuário e senha e devolve um token assinado, com validade. O token vai em toda chamada
// (api.ts); quando vence ou o backend recusa (401), a sessão local acaba e a tela de login volta.
import { useCallback, useEffect, useState } from 'react'
import { api, setAuth } from './api'
import type { Identity, LoginReply } from './types'

const SESSION_KEY = 'agent-squads.session.v2'

export const ROLE_LABEL: Record<string, string> = {
  credit_analyst: 'Analista de crédito',
  relationship_manager: 'Gerente comercial',
}

export const roleLabel = (role: string) => ROLE_LABEL[role] ?? role.replace(/_/g, ' ')

interface Stored {
  user: Identity
  token: string
  expires_at: number
}

function load(): Stored | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY)
    const s = raw ? (JSON.parse(raw) as Stored) : null
    return s && s.token && s.expires_at * 1000 > Date.now() ? s : null
  } catch {
    return null
  }
}

function forget() {
  try {
    localStorage.removeItem(SESSION_KEY)
  } catch {
    /* armazenamento indisponível */
  }
}

export function useSession() {
  const [session, setSession] = useState<Stored | null>(load)

  const drop = useCallback(() => {
    forget()
    setAuth(null)
    setSession(null)
  }, [])

  // o token passa a valer antes do primeiro render da área de trabalho
  if (session) setAuth(session.token, drop)

  useEffect(() => {
    if (!session) return
    const ms = session.expires_at * 1000 - Date.now()
    const timer = window.setTimeout(drop, Math.max(ms, 0))
    return () => window.clearTimeout(timer)
  }, [session, drop])

  const signIn = useCallback(
    (reply: LoginReply) => {
      const s: Stored = { user: reply.user, token: reply.access_token, expires_at: reply.expires_at }
      try {
        localStorage.setItem(SESSION_KEY, JSON.stringify(s))
      } catch {
        /* armazenamento indisponível: a sessão só vive nesta aba */
      }
      setAuth(s.token, drop)
      setSession(s)
    },
    [drop],
  )

  const signOut = useCallback(() => {
    void api.logout().catch(() => undefined)
    drop()
  }, [drop])

  return { user: session?.user ?? null, signIn, signOut }
}
