export type AppSelectValue = string | number

export interface AppSelectOption<T extends AppSelectValue = AppSelectValue> {
  label: string
  value: T
  description?: string
  disabled?: boolean
}

