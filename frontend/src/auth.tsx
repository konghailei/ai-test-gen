/**
 * 认证状态管理 — React Context
 *
 * 管理登录状态、Token 存储、用户信息。
 * Token 存储在 localStorage，页面刷新后自动恢复。
 * 所有 API 请求自动附加 Authorization Header。
 */

import { createContext, useContext, useState, useEffect, useCallback, type ReactNode } from 'react'

const TOKEN_KEY = 'aitestgen_token'
const USER_KEY = 'aitestgen_user'

export interface UserInfo {
  id: number
  username: string
  role: string
  permissions: string[]
  display_name: string
  email: string
}

interface AuthState {
  token: string | null
  user: UserInfo | null
  isLoggedIn: boolean
  login: (token: string, user: UserInfo) => void
  logout: () => void
  setUser: (user: UserInfo) => void
}

const AuthContext = createContext<AuthState>({
  token: null,
  user: null,
  isLoggedIn: false,
  login: () => {},
  logout: () => {},
  setUser: () => {},
})

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => localStorage.getItem(TOKEN_KEY))
  const [user, setUserState] = useState<UserInfo | null>(() => {
    try {
      const raw = localStorage.getItem(USER_KEY)
      return raw ? JSON.parse(raw) : null
    } catch {
      return null
    }
  })

  const login = useCallback((newToken: string, newUser: UserInfo) => {
    localStorage.setItem(TOKEN_KEY, newToken)
    localStorage.setItem(USER_KEY, JSON.stringify(newUser))
    setToken(newToken)
    setUserState(newUser)
  }, [])

  const logout = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem(USER_KEY)
    setToken(null)
    setUserState(null)
  }, [])

  const setUser = useCallback((u: UserInfo) => {
    localStorage.setItem(USER_KEY, JSON.stringify(u))
    setUserState(u)
  }, [])

  return (
    <AuthContext.Provider value={{
      token,
      user,
      isLoggedIn: !!token,
      login,
      logout,
      setUser,
    }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  return useContext(AuthContext)
}
