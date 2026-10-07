function fileBatchKey(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}:${file.type}`
}

function isUsableFile(file: File | null): file is File {
  return !!file && (!!file.name || file.size > 0)
}

function appendUniqueFile(files: File[], seen: Set<string>, file: File | null) {
  if (!isUsableFile(file)) return
  const key = fileBatchKey(file)
  if (seen.has(key)) return
  seen.add(key)
  files.push(file)
}

function itemIsDirectory(item: DataTransferItem): boolean {
  const entry = item.webkitGetAsEntry?.()
  return entry?.isDirectory === true
}

export function hasFileTransfer(dataTransfer: DataTransfer | null): boolean {
  if (!dataTransfer) return false
  if (Array.from(dataTransfer.types ?? []).includes('Files')) return true
  if (Array.from(dataTransfer.items ?? []).some((item) => item.kind === 'file')) return true
  return (dataTransfer.files?.length ?? 0) > 0
}

export function isDirectoryTransfer(dataTransfer: DataTransfer | null): boolean {
  if (!dataTransfer) return false
  return Array.from(dataTransfer.items ?? []).some((item) => item.kind === 'file' && itemIsDirectory(item))
}

export function extractDroppedFiles(dataTransfer: DataTransfer | null): File[] {
  if (!dataTransfer || isDirectoryTransfer(dataTransfer)) return []

  const files: File[] = []
  const seen = new Set<string>()

  for (const file of Array.from(dataTransfer.files ?? [])) {
    appendUniqueFile(files, seen, file)
  }

  for (const item of Array.from(dataTransfer.items ?? [])) {
    if (item.kind !== 'file') continue
    appendUniqueFile(files, seen, item.getAsFile())
  }

  return files
}
