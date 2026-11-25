import { useState, useEffect } from 'react';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Select, SelectContent, SelectGroup, SelectItem, SelectLabel, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Badge } from '@/components/ui/badge';
import { Loader2, Check } from 'lucide-react';
import { toast } from 'sonner';
import { getModelConfig, saveModelConfig, type ModelConfig, type ReasoningEffort } from '@/services/apiClient';

const SONNET_55 = 'claude-sonnet-5-5';
const DEFAULT_EFFORT: ReasoningEffort = 'medium';
const REASONING_EFFORTS = [
  { value: 'low', label: 'Low' },
  { value: 'medium', label: 'Medium' },
  { value: 'high', label: 'High' },
  { value: 'xhigh', label: 'Extra high' },
  { value: 'max', label: 'Maximum' },
] as const;

const MODELS = [
  { value: 'gpt-4o', label: 'GPT-4o', group: 'OpenAI' },
  { value: 'gpt-4o-mini', label: 'GPT-4o Mini', group: 'OpenAI' },
  { value: 'claude-sonnet-4-6', label: 'Claude Sonnet 4.6', group: 'Anthropic' },
  { value: SONNET_55, label: 'Claude Sonnet 5.5', group: 'Anthropic' },
  { value: 'claude-haiku-4-5-20251001', label: 'Claude Haiku 4.5', group: 'Anthropic' },
  { value: 'gemini-2.5-flash', label: 'Gemini 2.5 Flash', group: 'Google' },
  { value: 'arn:aws:bedrock:eu-north-1:713939170911:inference-profile/eu.anthropic.claude-sonnet-4-6', label: 'Claude Sonnet 4.6 (EU)', group: 'Amazon Bedrock' },
  { value: 'us.amazon.nova-pro-v1:0', label: 'Nova Pro', group: 'Amazon Bedrock' },
  { value: 'us.amazon.nova-lite-v1:0', label: 'Nova Lite', group: 'Amazon Bedrock' },
  { value: 'us.amazon.nova-micro-v1:0', label: 'Nova Micro', group: 'Amazon Bedrock' },
  { value: 'us.meta.llama3-1-70b-instruct-v1:0', label: 'Llama 3.1 70B', group: 'Amazon Bedrock' },
] as const;

const GROUPS = ['OpenAI', 'Anthropic', 'Google', 'Amazon Bedrock'] as const;

function getProviderFromModel(model: string): string {
  if (model.startsWith('claude-')) return 'Anthropic';
  if (model.startsWith('gemini-')) return 'Google';
  // Bedrock model IDs always contain a colon (e.g. "us.amazon.nova-pro-v1:0")
  if (model.includes(':')) return 'Amazon Bedrock';
  return 'OpenAI';
}

function getProviderColor(provider: string): string {
  switch (provider) {
    case 'Anthropic': return 'bg-orange-100 text-orange-800 dark:bg-orange-900 dark:text-orange-200';
    case 'Google': return 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200';
    case 'Amazon Bedrock': return 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200';
    default: return 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200';
  }
}

export default function ModelConfigPage() {
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const [config, setConfig] = useState<ModelConfig>({
    model: 'claude-sonnet-4-6',
    temperature: 0.2,
    top_p: 0.9,
    max_tokens: 4096,
  });

  useEffect(() => {
    loadConfig();
  }, []);

  const loadConfig = async () => {
    try {
      setLoading(true);
      const data = await getModelConfig();
      if (data.config) {
        setConfig(data.config);
      }
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to load model config');
    } finally {
      setLoading(false);
    }
  };

  const handleSave = async () => {
    try {
      setSaving(true);
      const { effort, ...settings } = config;
      await saveModelConfig(
        settings.model === SONNET_55 ? { ...settings, effort: effort ?? DEFAULT_EFFORT } : settings,
      );
      setSaved(true);
      toast.success('Model configuration saved');
      setTimeout(() => setSaved(false), 2000);
    } catch (err) {
      toast.error(err instanceof Error ? err.message : 'Failed to save model config');
    } finally {
      setSaving(false);
    }
  };

  const provider = getProviderFromModel(config.model);
  const isSonnet55 = config.model === SONNET_55;

  if (loading) {
    return (
      <div className="flex justify-center py-16">
        <Loader2 className="h-8 w-8 animate-spin" />
      </div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-2xl space-y-6">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-1">
          <h1 className="text-2xl font-semibold tracking-tight">Model Configuration</h1>
          <p className="text-sm text-muted-foreground">
            Choose the AI model and tune how it generates trading decisions.
          </p>
        </div>
        <Badge className={getProviderColor(provider)}>{provider}</Badge>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>AI Model</CardTitle>
        </CardHeader>
        <CardContent className="space-y-6">
          {/* Model Selection */}
          <div className="space-y-1.5">
            <Label htmlFor="model-select">Model</Label>
            <Select
              value={config.model}
              onValueChange={(value) => setConfig({ ...config, model: value })}
            >
              <SelectTrigger id="model-select">
                <SelectValue placeholder="Select a model" />
              </SelectTrigger>
              <SelectContent>
                {GROUPS.map((group) => (
                  <SelectGroup key={group}>
                    <SelectLabel>{group}</SelectLabel>
                    {MODELS.filter((m) => m.group === group).map((m) => (
                      <SelectItem key={m.value} value={m.value}>
                        {m.label}
                      </SelectItem>
                    ))}
                  </SelectGroup>
                ))}
              </SelectContent>
            </Select>
            <p className="text-xs text-muted-foreground">
              The selection applies to the next analysis cycle and to new backtests that use the saved model.
            </p>
          </div>

          {/* Generation parameters */}
          <div className="space-y-6 border-t border-border pt-6">
            {isSonnet55 ? (
              <div className="space-y-1.5">
                <Label htmlFor="reasoning-effort">Reasoning Effort</Label>
                <Select
                  value={config.effort ?? DEFAULT_EFFORT}
                  onValueChange={(value: ReasoningEffort) => setConfig({ ...config, effort: value })}
                >
                  <SelectTrigger id="reasoning-effort">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {REASONING_EFFORTS.map(({ value, label }) => (
                      <SelectItem key={value} value={value}>{label}</SelectItem>
                    ))}
                  </SelectContent>
                </Select>
                <p className="text-xs text-muted-foreground">
                  Applies to the expert and judge. Higher effort can take longer and use more billed tokens.
                </p>
              </div>
            ) : (
              <>
                <SliderRow
                  id="temperature"
                  label="Temperature"
                  description="Higher values make output more random, lower values more focused."
                  value={config.temperature}
                  min={0}
                  max={2}
                  step={0.1}
                  displayValue={config.temperature.toFixed(1)}
                  minLabel="Deterministic (0)"
                  maxLabel="Creative (2)"
                  onChange={(v) => setConfig({ ...config, temperature: v })}
                />
                <SliderRow
                  id="top-p"
                  label="Top P"
                  description="Limits sampling to the most likely tokens (nucleus sampling)."
                  value={config.top_p}
                  min={0}
                  max={1}
                  step={0.05}
                  displayValue={config.top_p.toFixed(2)}
                  minLabel="Focused (0)"
                  maxLabel="Diverse (1)"
                  onChange={(v) => setConfig({ ...config, top_p: v })}
                />
              </>
            )}

            <SliderRow
              id="max-tokens"
              label="Max Tokens"
              description={isSonnet55
                ? 'Maximum tokens per call, including reasoning and the final response.'
                : 'Maximum number of tokens the model may generate per response.'}
              value={config.max_tokens}
              min={256}
              max={32768}
              step={256}
              displayValue={config.max_tokens.toLocaleString()}
              minLabel="256"
              maxLabel="32,768"
              onChange={(v) => setConfig({ ...config, max_tokens: v })}
            />
          </div>

          {/* Save Button */}
          <div className="flex justify-end border-t border-border pt-6">
            <Button onClick={handleSave} disabled={saving} className="min-w-[10rem]">
              {saving ? (
                <Loader2 className="h-4 w-4 animate-spin mr-2" />
              ) : saved && (
                <Check className="h-4 w-4 mr-2" />
              )}
              {saving ? 'Saving...' : saved ? 'Saved' : 'Save Configuration'}
            </Button>
          </div>
        </CardContent>
      </Card>
    </div>
  );
}

interface SliderRowProps {
  id: string;
  label: string;
  description: string;
  value: number;
  min: number;
  max: number;
  step: number;
  displayValue: string;
  minLabel: string;
  maxLabel: string;
  onChange: (value: number) => void;
}

function SliderRow({
  id,
  label,
  description,
  value,
  min,
  max,
  step,
  displayValue,
  minLabel,
  maxLabel,
  onChange,
}: SliderRowProps) {
  const pct = ((value - min) / (max - min)) * 100;
  const fill = `linear-gradient(to right, hsl(var(--color-primary)) ${pct}%, hsl(var(--color-muted)) ${pct}%)`;

  return (
    <div className="space-y-2">
      <div className="flex items-baseline justify-between gap-4">
        <Label htmlFor={id}>{label}</Label>
        <span className="rounded-md bg-secondary px-2 py-0.5 font-mono text-sm tabular-nums text-foreground">
          {displayValue}
        </span>
      </div>
      <p className="text-xs text-muted-foreground">{description}</p>
      <input
        type="range"
        id={id}
        className="ui-slider"
        style={{ background: fill }}
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(parseFloat(e.target.value))}
      />
      <div className="flex justify-between text-xs text-muted-foreground">
        <span>{minLabel}</span>
        <span>{maxLabel}</span>
      </div>
    </div>
  );
}
