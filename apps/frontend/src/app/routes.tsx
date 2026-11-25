import {
  lazy,
  Suspense,
  type ComponentType,
  type LazyExoticComponent
} from "react"
import { createBrowserRouter, RouterProvider, Navigate } from "react-router-dom"
import Layout from "@/app/layout"
import RouteError from "@/app/RouteError"
import { PageSkeleton } from "@/components/page-skeleton"

const Dashboard = lazy(() => import("@/pages/Dashboard"))
const Home = lazy(() => import("@/pages/Home"))
const Agents = lazy(() => import("@/pages/agents"))
const FlagsConfigs = lazy(() => import("@/pages/configs/flags"))
const ModelSelection = lazy(() => import("@/pages/configs/model-selection"))
const UnifiedAnalysis = lazy(() => import("@/pages/UnifiedAnalysis"))
const CycleDetails = lazy(() => import("@/pages/CycleDetails"))
const AiDecisionDetails = lazy(() => import("@/pages/AiDecisionDetails"))
const Logs = lazy(() => import("@/pages/Logs"))
const Admin = lazy(() => import("@/pages/Admin"))
const BacktestList = lazy(() => import("@/pages/BacktestList"))
const BacktestCreate = lazy(() => import("@/pages/BacktestCreate"))
const BacktestDetails = lazy(() => import("@/pages/BacktestDetails"))
const About = lazy(() => import("@/pages/About"))

const withSuspense = (
  Component: LazyExoticComponent<ComponentType>
) => (
  <Suspense fallback={<PageSkeleton />}>
    <Component />
  </Suspense>
)

const router = createBrowserRouter([
  {
    path: "/",
    element: <Layout />,
    errorElement: <RouteError />,
    children: [
      { index: true, element: <Navigate to="/dashboard" replace /> },
      { path: "dashboard", element: withSuspense(Dashboard) },
      { path: "home", element: withSuspense(Home) },
      { path: "agents", element: withSuspense(Agents) },
      // Broker connection now lives on the Agents page; keep the old path working.
      { path: "configs/brokers", element: <Navigate to="/agents" replace /> },
      { path: "configs/flags", element: withSuspense(FlagsConfigs) },
      { path: "configs/model-selection", element: withSuspense(ModelSelection) },
      // Legacy redirect: old GPT Prompts path now points to Model Selection
      { path: "configs/gpt-prompts", element: <Navigate to="/configs/model-selection" replace /> },
      { path: "unified-analysis", element: withSuspense(UnifiedAnalysis) },
      { path: "cycle/:cycleId", element: withSuspense(CycleDetails) },
      { path: "ai-decision/:responseId", element: withSuspense(AiDecisionDetails) },
      { path: "logs", element: withSuspense(Logs) },
      { path: "metrics", element: <Navigate to="/dashboard" replace /> },
      { path: "admin", element: withSuspense(Admin) },
      { path: "backtests", element: withSuspense(BacktestList) },
      { path: "backtests/create", element: withSuspense(BacktestCreate) },
      { path: "backtests/:backtestId", element: withSuspense(BacktestDetails) }
    ]
  },
  {
    path: "/about",
    element: withSuspense(About),
    errorElement: <RouteError />
  }
])

export function AppRouter() {
  return <RouterProvider router={router} />
}
