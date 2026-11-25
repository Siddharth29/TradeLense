import { useEffect, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  killSwitch,
  getReadiness,
  getHealth,
  getAdminConfig,
  putAdminConfig,
} from "@/services/apiClient"
import type { Readiness, HealthResponse, AdminConfig } from "@/types/api"
import { Button } from "@/components/ui/button"
import ConfigHistory from "@/components/ConfigHistory"
import { Loader2 } from "lucide-react"
import { toast } from "sonner"

export default function Admin() {
  const qc = useQueryClient()

  const { data: readiness } = useQuery<Readiness>({ queryKey: ["readiness"], queryFn: getReadiness })
  const { data: health } = useQuery<HealthResponse>({ queryKey: ["health"], queryFn: getHealth })
  const {
    data: adminConfig,
    isError: isAdminError,
    error: adminError,
  } = useQuery({ queryKey: ["adminConfig"], queryFn: getAdminConfig })

  const [adminEnv, setAdminEnv] = useState("")
  const [showHistory, setShowHistory] = useState(false)
  const env = import.meta.env.VITE_ENV || ""

  useEffect(() => {
    if (adminConfig) setAdminEnv(adminConfig.env)
  }, [adminConfig])

  const saveAdmin = useMutation({
    mutationFn: (cfg: AdminConfig) => putAdminConfig(cfg),
    retry: 2,
    onMutate: async (cfg) => {
      await qc.cancelQueries({ queryKey: ["adminConfig"] })
      const prev = qc.getQueryData<AdminConfig>(["adminConfig"])
      qc.setQueryData<AdminConfig>(["adminConfig"], cfg)
      return { prev }
    },
    onError: (err: Error, _vars, ctx) => {
      if (ctx?.prev) qc.setQueryData(["adminConfig"], ctx.prev)
      toast.error(err.message)
    },
    onSuccess: () => toast.success("Admin config saved"),
    onSettled: () => qc.invalidateQueries({ queryKey: ["adminConfig"] }),
  })

  const clockSkew = health?.time ? Math.abs(Date.now() - Date.parse(health.time)) : undefined

  const kill = useMutation({
    mutationFn: killSwitch,
    retry: 2,
    onSuccess: () => toast.success("Kill switch triggered"),
    onError: (err: Error) => toast.error(err.message),
  })

  const onSaveAdmin = () => {
    if (!adminConfig) return
    saveAdmin.mutate({ ...adminConfig, env: adminEnv })
  }

  return (
    <div className="space-y-6">
      <h1 className="text-xl font-bold">Admin</h1>

      <section>
        <h2 className="mb-2 font-semibold">Readiness</h2>
        {readiness && (
          <div className="space-y-2 text-sm">
            <div>Status: {readiness.ready ? "Ready" : "Not Ready"}</div>
            {Object.keys(readiness.blockers || {}).length > 0 && (
              <ul className="list-disc pl-5">
                {Object.entries(readiness.blockers).map(([k, v]) => (
                  <li key={k} className="text-red-600">
                    {k}: {v}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {clockSkew != null && (
          <div className="mt-2 text-sm">Clock skew: {clockSkew}ms</div>
        )}
      </section>
      <section className="space-y-4">
        <div className="flex justify-end">
          <Button variant="outline" size="sm" onClick={() => setShowHistory(true)}>
            View history
          </Button>
        </div>
        {isAdminError && (
          <div className="mb-sm rounded border border-destructive/50 bg-destructive/10 p-sm text-sm text-destructive">
            {(adminError as Error)?.message || "Failed to load admin config"}
          </div>
        )}
        <label className="flex flex-col gap-xs">
          <span>Environment</span>
          <select
            value={adminEnv}
            onChange={(e) => setAdminEnv(e.target.value)}
            className="border p-xs"
          >
            <option value="dev">dev</option>
            <option value="prod">prod</option>
          </select>
        </label>
        <div>
          <Button type="button" onClick={onSaveAdmin} disabled={saveAdmin.isPending}>
            {saveAdmin.isPending ? <Loader2 className="h-4 w-4 animate-spin" /> : "Save"}
          </Button>
        </div>
        {showHistory && (
          <ConfigHistory
            domain="admin"
            env={env}
            open={showHistory}
            onClose={() => setShowHistory(false)}
            queryKey={['adminConfig']}
          />
        )}
      </section>

      <section>
        <button
          className="rounded bg-red-600 px-md py-sm text-white"
          onClick={() => kill.mutate()}
          disabled={kill.isPending}
        >
          Kill Switch
        </button>
      </section>
    </div>
  )
}
