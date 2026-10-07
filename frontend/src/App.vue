<template>
  <n-config-provider :theme-overrides="themeOverrides">
    <n-message-provider placement="top" container-class="testagent-toast-container" :max="4" keep-alive-on-hover>
      <router-view />
    </n-message-provider>
  </n-config-provider>
</template>

<script setup lang="ts">
import { NConfigProvider, NMessageProvider, type GlobalThemeOverrides } from 'naive-ui'

const themeOverrides: GlobalThemeOverrides = {
  common: {
    primaryColor: '#000000',
    primaryColorHover: '#1f2937',
    primaryColorPressed: '#111111',
    borderRadius: '8px',
    fontFamily:
      'Inter, "PingFang SC", "Microsoft YaHei", "Noto Sans SC", system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif',
    fontFamilyMono: '"SFMono-Regular", "Cascadia Code", "Roboto Mono", Consolas, "Liberation Mono", monospace',
    fontWeight: '400',
    fontWeightStrong: '500'
  },
  Button: {
    borderRadiusMedium: '8px',
    heightMedium: '36px',
    fontSizeMedium: '14px',
    fontWeight: '500'
  },
  Input: {
    borderRadius: '8px',
    fontSizeMedium: '15px'
  },
  Form: {
    labelFontSizeTopMedium: '13px',
    labelFontWeight: '500'
  },
  DataTable: {
    fontSizeMedium: '14px',
    thFontWeight: '500'
  },
  Tag: {
    fontSizeMedium: '12px'
  },
  Card: {
    borderRadius: '12px'
  },
  Message: {
    color: '#ffffff',
    colorInfo: '#ffffff',
    colorSuccess: '#ffffff',
    colorWarning: '#ffffff',
    colorError: '#ffffff',
    colorLoading: '#ffffff',
    textColor: '#242424',
    textColorInfo: '#242424',
    textColorSuccess: '#242424',
    textColorWarning: '#242424',
    textColorError: '#242424',
    textColorLoading: '#242424',
    iconColorInfo: '#52525b',
    iconColorSuccess: '#2f7d61',
    iconColorWarning: '#95661f',
    iconColorError: '#c9444d',
    iconColorLoading: '#52525b',
    borderRadius: '14px',
    padding: '12px 16px',
    margin: '0 0 10px 0',
    iconMargin: '0 10px 0 0',
    iconSize: '19px',
    fontSize: '14px',
    lineHeight: '22px',
    maxWidth: 'min(420px, calc(100vw - 32px))',
    boxShadow: '0 16px 40px rgba(24, 24, 27, 0.14), inset 0 0 0 1px rgba(24, 24, 27, 0.08)',
    boxShadowInfo: '0 16px 40px rgba(24, 24, 27, 0.14), inset 0 0 0 1px rgba(24, 24, 27, 0.08)',
    boxShadowSuccess: '0 16px 40px rgba(24, 24, 27, 0.14), inset 0 0 0 1px rgba(24, 24, 27, 0.08)',
    boxShadowWarning: '0 16px 40px rgba(24, 24, 27, 0.14), inset 0 0 0 1px rgba(24, 24, 27, 0.08)',
    boxShadowError: '0 16px 40px rgba(24, 24, 27, 0.14), inset 0 0 0 1px rgba(24, 24, 27, 0.08)',
    boxShadowLoading: '0 16px 40px rgba(24, 24, 27, 0.14), inset 0 0 0 1px rgba(24, 24, 27, 0.08)'
  }
}
</script>

<style>
.testagent-toast-container {
  top: 20px !important;
}

.testagent-toast-container .n-message {
  width: fit-content;
  min-width: 0;
  justify-content: center;
  font-weight: var(--font-medium);
  letter-spacing: -0.01em;
}

.testagent-toast-container .n-message__content {
  text-align: center;
  text-wrap: pretty;
}
</style>
