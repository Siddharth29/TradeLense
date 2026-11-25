import { Badge } from '@/components/ui/badge'
import { CheckCircle, XCircle, AlertTriangle, Clock, Minus } from 'lucide-react'

interface StatusBadgeProps {
  status: 'valid' | 'invalid' | 'warning' | 'pending' | 'neutral'
  label: string
  size?: 'sm' | 'md' | 'lg'
  showIcon?: boolean
}

export function StatusBadge({ 
  status, 
  label, 
  size = 'md', 
  showIcon = true 
}: StatusBadgeProps) {
  const getStatusConfig = () => {
    switch (status) {
      case 'valid':
        return {
          variant: 'default' as const,
          className: 'bg-green-100 text-green-800 border-green-200',
          icon: <CheckCircle className="h-3 w-3" />
        }
      case 'invalid':
        return {
          variant: 'destructive' as const,
          className: 'bg-red-100 text-red-800 border-red-200',
          icon: <XCircle className="h-3 w-3" />
        }
      case 'warning':
        return {
          variant: 'secondary' as const,
          className: 'bg-yellow-100 text-yellow-800 border-yellow-200',
          icon: <AlertTriangle className="h-3 w-3" />
        }
      case 'pending':
        return {
          variant: 'outline' as const,
          className: 'bg-blue-100 text-blue-800 border-blue-200',
          icon: <Clock className="h-3 w-3" />
        }
      case 'neutral':
        return {
          variant: 'outline' as const,
          className: 'bg-gray-100 text-gray-800 border-gray-200',
          icon: <Minus className="h-3 w-3" />
        }
    }
  }

  const config = getStatusConfig()
  const sizeClasses = {
    sm: 'text-xs px-2 py-1',
    md: 'text-sm px-2.5 py-1.5',
    lg: 'text-base px-3 py-2'
  }

  return (
    <Badge 
      variant={config.variant}
      className={`${config.className} ${sizeClasses[size]} flex items-center gap-1`}
    >
      {showIcon && config.icon}
      {label}
    </Badge>
  )
}
