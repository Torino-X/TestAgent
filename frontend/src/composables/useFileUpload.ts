/**
 * File upload composable.
 *
 * Provides reactive helpers for file input, drag-and-drop, and upload
 * progress. Currently a placeholder — wires up the fileStore.uploadFile
 * action with simulated progress.
 */

import { ref, type Ref } from 'vue'
import type { FileAttachment } from '@/types'
import { useFileStore } from '@/stores/fileStore'

export interface UseFileUploadReturn {
  selectedFiles: Ref<File[]>
  progress: Ref<number>
  uploading: Ref<boolean>
  pickFiles: () => void
  handleDrop: (event: DragEvent) => void
  uploadAll: () => Promise<FileAttachment[]>
  clearSelection: () => void
}

export function useFileUpload(): UseFileUploadReturn {
  const fileStore = useFileStore()
  const selectedFiles = ref<File[]>([])
  const progress = ref(0)
  const uploading = ref(false)

  /** Open the native file picker (placeholder — creates a hidden input). */
  function pickFiles() {
    const input = document.createElement('input')
    input.type = 'file'
    input.multiple = true
    input.accept = '.docx,.txt,.md'
    input.onchange = () => {
      if (input.files) {
        selectedFiles.value = Array.from(input.files)
      }
    }
    input.click()
  }

  /** Handle drag-and-drop files. */
  function handleDrop(event: DragEvent) {
    event.preventDefault()
    if (event.dataTransfer?.files) {
      selectedFiles.value = Array.from(event.dataTransfer.files)
    }
  }

  /** Upload all selected files via fileStore. */
  async function uploadAll(): Promise<FileAttachment[]> {
    uploading.value = true
    progress.value = 0
    const results: FileAttachment[] = []

    for (let i = 0; i < selectedFiles.value.length; i++) {
      const uploaded = await fileStore.uploadFile(selectedFiles.value[i])
      results.push(uploaded)
      progress.value = Math.round(((i + 1) / selectedFiles.value.length) * 100)
    }

    uploading.value = false
    selectedFiles.value = []
    return results
  }

  function clearSelection() {
    selectedFiles.value = []
    progress.value = 0
  }

  return {
    selectedFiles,
    progress,
    uploading,
    pickFiles,
    handleDrop,
    uploadAll,
    clearSelection
  }
}
