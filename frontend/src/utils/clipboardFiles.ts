function isUsableFile(file: File | null): file is File {
  return !!file && (!!file.name || file.size > 0)
}

function fileBatchKey(file: File): string {
  return `${file.name}:${file.size}:${file.lastModified}:${file.type}`
}

function appendUniqueFile(files: File[], seen: Set<string>, file: File | null) {
  if (!isUsableFile(file)) return
  const key = fileBatchKey(file)
  if (seen.has(key)) return
  seen.add(key)
  files.push(file)
}

export function extractClipboardFiles(event: ClipboardEvent): File[] {
  const clipboard = event.clipboardData
  if (!clipboard) return []

  const files: File[] = []
  const seen = new Set<string>()

  for (const file of Array.from(clipboard.files ?? [])) {
    appendUniqueFile(files, seen, file)
  }

  for (const item of Array.from(clipboard.items ?? [])) {
    if (item.kind !== 'file') continue
    appendUniqueFile(files, seen, item.getAsFile())
  }

  return files
}
