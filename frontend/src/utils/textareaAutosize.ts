export interface TextareaAutosizeOptions {
  minHeight: number
  maxHeight: number
}

export function resizeTextareaToContent(
  textarea: HTMLTextAreaElement,
  options: TextareaAutosizeOptions
) {
  textarea.style.height = 'auto'
  const contentHeight = Math.max(options.minHeight, textarea.scrollHeight)
  const nextHeight = Math.min(contentHeight, options.maxHeight)
  textarea.style.height = `${nextHeight}px`
  textarea.style.overflowY = contentHeight > options.maxHeight ? 'auto' : 'hidden'
}

export function resetTextareaHeight(
  textarea: HTMLTextAreaElement,
  options: TextareaAutosizeOptions
) {
  textarea.style.height = `${options.minHeight}px`
  textarea.style.overflowY = 'hidden'
}
