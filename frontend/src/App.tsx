import { useLayoutEffect, type ReactNode } from 'react'
import { Navigate, Outlet, Route, Routes, useLocation, useParams } from 'react-router-dom'
import { Header } from './components/Header'
import { ProductionProvider } from './components/ProductionProvider'
import { useProduction } from './hooks/useProduction'
import { useAuthReady, useAuthUser } from './lib/auth'
import { Auth } from './routes/Auth'
import { ForgotPassword, ResetPassword } from './routes/Auth/PasswordRecovery'
import { Account } from './routes/Account'
import { Checkout } from './routes/Checkout'
import { Home } from './routes/Home'
import { Landing } from './routes/Landing'
import { Library } from './routes/Library'
import { PrivacyPolicy, RefundPolicy, Support, TermsOfService } from './routes/Legal'
import { Plans } from './routes/Plans'
import { Production } from './routes/Production'
import { VideoDetail } from './routes/VideoDetail'
import styles from './App.module.css'

function StudioLayout() {
  const location = useLocation()
  const { generating, job } = useProduction()
  const activeScene = job?.scenes.find((scene) => scene.status === 'active')
    ?? [...(job?.scenes ?? [])].reverse().find((scene) => scene.status === 'done')

  return (
    <>
      <Header
        jobActive={generating}
        jobScene={activeScene?.title || job?.detail || 'Creating explanation'}
        jobProgress={Math.max(3, job?.percent ?? 0)}
      />
      <main><div className={styles.screen} key={location.pathname}><Outlet /></div></main>
    </>
  )
}

function RequireAuth({ children }: { children: ReactNode }) {
  const ready = useAuthReady()
  const user = useAuthUser()
  const location = useLocation()

  if (!ready) return <main className={styles.authLoading}>Restoring your studio…</main>
  if (!user) {
    const returnTo = `${location.pathname}${location.search}`
    return <Navigate replace to={`/auth?returnTo=${encodeURIComponent(returnTo)}`} />
  }
  return children
}

function PlanThemeSync() {
  const user = useAuthUser()

  useLayoutEffect(() => {
    document.documentElement.dataset.planTheme = user?.plan === 'Pro Ultra'
      ? 'pro-ultra'
      : user?.plan === 'Pro'
        ? 'pro'
        : 'free'
  }, [user?.plan])

  return null
}

function LegacyRedirect({ to }: { to: string }) {
  const location = useLocation()
  return <Navigate replace to={`${to}${location.search}`} />
}

function LegacyVideoRedirect() {
  const { id = '' } = useParams()
  return <Navigate replace to={`/app/videos/${encodeURIComponent(id)}`} />
}

function AppRoutes() {
  const location = useLocation()

  useLayoutEffect(() => {
    if (!location.hash) window.scrollTo(0, 0)
  }, [location.hash, location.pathname])

  return (
    <Routes>
      <Route path="/" element={<Landing />} />
      <Route path="/auth" element={<Auth />} />
      <Route path="/auth/forgot" element={<ForgotPassword />} />
      <Route path="/auth/recovery" element={<ResetPassword />} />
      <Route path="/checkout" element={<Checkout />} />
      <Route path="/privacy" element={<PrivacyPolicy />} />
      <Route path="/terms" element={<TermsOfService />} />
      <Route path="/refunds" element={<RefundPolicy />} />
      <Route path="/support" element={<Support />} />
      <Route path="/privacy-policy" element={<Navigate replace to="/privacy" />} />
      <Route path="/terms-of-service" element={<Navigate replace to="/terms" />} />
      <Route path="/refund-policy" element={<Navigate replace to="/refunds" />} />
      <Route path="/app" element={<RequireAuth><StudioLayout /></RequireAuth>}>
        <Route index element={<Home />} />
        <Route path="account" element={<Account />} />
        <Route path="plans" element={<Plans />} />
        <Route path="create" element={<Production />} />
        <Route path="videos" element={<Library />} />
        <Route path="videos/:id" element={<VideoDetail />} />
      </Route>
      <Route path="/create" element={<LegacyRedirect to="/app/create" />} />
      <Route path="/videos" element={<LegacyRedirect to="/app/videos" />} />
      <Route path="/videos/:id" element={<LegacyVideoRedirect />} />
      <Route path="*" element={<Navigate replace to="/" />} />
    </Routes>
  )
}

function App() {
  return <><PlanThemeSync /><ProductionProvider><AppRoutes /></ProductionProvider></>
}

export default App
