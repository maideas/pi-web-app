import { defineConfig } from 'vite'
import { svelte } from '@sveltejs/vite-plugin-svelte'

// https://vite.dev/config/
export default defineConfig({
  plugins: [svelte()],
  server: {
    proxy: {
      '/events': 'http://127.0.0.1:5000',
      '/prompt': 'http://127.0.0.1:5000',
      '/abort': 'http://127.0.0.1:5000',
      '/new_session': 'http://127.0.0.1:5000',
      '/messages': 'http://127.0.0.1:5000',
      '/state': 'http://127.0.0.1:5000',
      '/ui-response': 'http://127.0.0.1:5000',
      '/stats': 'http://127.0.0.1:5000',
      '/models': 'http://127.0.0.1:5000',
      '/set_model': 'http://127.0.0.1:5000',
      '/set_thinking': 'http://127.0.0.1:5000',
      '/thinking_levels': 'http://127.0.0.1:5000',
      '/sessions': 'http://127.0.0.1:5000',
      '/switch_session': 'http://127.0.0.1:5000',
      '/commands': 'http://127.0.0.1:5000',
      '/compact': 'http://127.0.0.1:5000',
      '/set_session_name': 'http://127.0.0.1:5000',
      '/export_html': 'http://127.0.0.1:5000',
      '/api': 'http://127.0.0.1:5000',
      '/download': 'http://127.0.0.1:5000',
    },
  },
})
