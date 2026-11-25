// Keep in sync with packages/core/asset_registry.py ASSET_PROFILES.
export const Asset = {
  XAUUSD: 'XAUUSD',
  XAGUSD: 'XAGUSD',
  BTCUSD: 'BTCUSD',
  ETHUSD: 'ETHUSD',
  GBPUSD: 'GBPUSD',
  EURUSD: 'EURUSD',
  USOIL: 'USOIL',
  USTEC: 'USTEC',
} as const;

export type Asset = typeof Asset[keyof typeof Asset];

export const assetOptions = Object.values(Asset);
