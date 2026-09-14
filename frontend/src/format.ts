export function formatCostCny(value: number | null | undefined) {
  if (value == null) return '—'
  const decimalPlaces = value > 0 && value < 0.01 ? 4 : 2
  return `¥${value.toFixed(decimalPlaces)}`
}
