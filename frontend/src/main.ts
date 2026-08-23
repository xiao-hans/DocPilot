import { createApp } from 'vue'
import App from './App.vue'

// 本地字体（fontsource 包随构建打包，禁止 CDN，适配离线 Demo 录制）
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import '@fontsource/inter/700.css'
import '@fontsource/open-sans/400.css'
import '@fontsource/open-sans/600.css'
import '@fontsource/inconsolata/400.css'
import '@fontsource/inconsolata/500.css'
import '@fontsource/inconsolata/600.css'

import './assets/tokens.css'
import './assets/app.css'

createApp(App).mount('#app')