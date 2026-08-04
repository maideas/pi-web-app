<script>
  import { onMount, tick } from 'svelte'
  import { marked } from 'marked'
  import hljs from 'highlight.js'
  import hljsDark from 'highlight.js/styles/github-dark.css?inline'
  import hljsLight from 'highlight.js/styles/github.css?inline'

  // Inject hljs themes scoped per app theme so they don't clash
  {
    const style = document.createElement('style')
    style.textContent =
      hljsDark.replaceAll('.hljs', '[data-theme="dark"] .hljs') +
      '\n' +
      hljsLight.replaceAll('.hljs', '[data-theme="light"] .hljs')
    document.head.appendChild(style)
  }

  // Theme (persisted). 'claude' is a light-theme variant applied via
  // data-contrast while data-theme stays "light" — so the markdown
  // preview palette (which only knows light/dark) keeps rendering the
  // light variant. Unknown persisted values (e.g. a removed theme)
  // fall back to light.
  const THEME_VARIANTS = { claude: 'claude' }
  const KNOWN_THEMES = ['light', 'dark', ...Object.keys(THEME_VARIANTS)]
  const savedTheme = localStorage.getItem('theme')
  let theme = $state(KNOWN_THEMES.includes(savedTheme) ? savedTheme : 'light')
  $effect(() => {
    const variant = THEME_VARIANTS[theme]
    document.documentElement.dataset.theme = variant ? 'light' : theme
    if (variant) document.documentElement.dataset.contrast = variant
    else delete document.documentElement.dataset.contrast
    localStorage.setItem('theme', theme)
  })

  function escapeHtml(text) {
    const div = document.createElement('div')
    div.textContent = text
    return div.innerHTML
  }

  // Syntax-highlight fenced code blocks in chat markdown
  marked.use({
    renderer: {
      code({ text, lang }) {
        const language = lang && hljs.getLanguage(lang) ? lang : null
        const html = language
          ? hljs.highlight(text, { language }).value
          : escapeHtml(text)
        // Language hint shown as a muted label above the block (via CSS)
        const hint = lang ? ` data-lang="${escapeHtml(lang)}"` : ''
        return `<pre${hint}><code class="hljs">${html}</code></pre>`
      },
      html(token) {
        return escapeHtml(token.text || '')
      },
    },
  })

  function highlight(text, path) {
    const ext = path?.split('.').pop()?.toLowerCase()
    try {
      if (ext && hljs.getLanguage(ext)) {
        return hljs.highlight(text, { language: ext }).value
      }
      if (text.length > 100_000) {
        return escapeHtml(text.slice(0, 100_000)) + '\n\n…(truncated)'
      }
      return hljs.highlightAuto(text).value
    } catch {
      // fall back to escaped plain text
      return escapeHtml(text)
    }
  }

  // Chat entries: { role: 'user'|'assistant'|'system', text, thinking?, images? } | { role: 'tool', ... }
  let entries = $state([])
  let input = $state('')
  let streaming = $state(false)
  let chatEl
  let inputEl

  // Model / thinking state
  let models = $state([])
  let currentModel = $state(null)
  let thinkingLevels = $state(['off'])
  let thinkingLevel = $state('off')

  // Session list
  let sessions = $state([])
  let currentSessionPath = $state('')

  // Project list / switcher. The active project is server-global: switching
  // respawns the pi subprocess with the project dir as cwd (killing the
  // running chat session, like an app restart).
  let projects = $state([])
  let currentProjectId = $state('')
  let showNewProject = $state(false)
  let showManage = $state(false) // manage-projects popup (detach)

  // New-project directory picker (popup). Shows only directories and is
  // confined to the parent directory of the current project (users of a
  // web app usually don't know absolute paths, so they browse instead).
  let npBase = $state('')
  let npPath = $state('')
  let npParent = $state(null)
  let npEntries = $state([])
  let npFolder = $state('')
  let npGit = $state(false)

  // Auto-close the project dialogs when unused: closes after
  // DIALOG_IDLE_MS without interaction (any input/focus inside the
  // dialog resets the timer) or when the chat input gains focus.
  const DIALOG_IDLE_MS = 20_000
  let dialogTimer = null
  function pokeDialogTimer() {
    clearTimeout(dialogTimer)
    dialogTimer = setTimeout(() => {
      showNewProject = false
      showManage = false
    }, DIALOG_IDLE_MS)
  }
  $effect(() => {
    if (showNewProject || showManage) pokeDialogTimer()
    else clearTimeout(dialogTimer)
  })

  // Slash commands: client-side built-ins + pi commands (extension/skill/template)
  // fetched from /commands. Built-in TUI commands (/model, /resume, ...) are NOT
  // recognized via RPC and get intercepted/blocked in handleSlashCommand.
  const BUILTIN_COMMANDS = [
    { name: 'new', description: 'Start a new session' },
    { name: 'abort', description: 'Stop the current run' },
    { name: 'compact', description: 'Compact context (optional instructions)' },
    { name: 'name', description: 'Set session display name' },
    { name: 'export', description: 'Export session to HTML' },
  ]
  let slashCommands = $state([]) // [{ name, description, source }]
  let slashSuggestions = $derived(
    input.startsWith('/') && !input.includes(' ')
      ? [...BUILTIN_COMMANDS, ...slashCommands]
          .filter((c) => c.name.startsWith(input.slice(1)))
      : []
  )
  // Keyboard navigation in the slash menu: ArrowUp/ArrowDown move the
  // selection, Enter (and Tab) insert it. Resets whenever the
  // suggestion list changes.
  let slashIndex = $state(0)
  $effect(() => {
    slashSuggestions // track
    slashIndex = 0
  })
  $effect(() => {
    slashIndex // track
    document.querySelector('.slash-item.selected')?.scrollIntoView({ block: 'nearest' })
  })

  // Footer stats
  let stats = $state(null)

  // Pending attachments: [{ kind: 'image', name, data (base64), mimeType, url (data URL) }
  //                       | { kind: 'text', name, text }]
  let attachments = $state([])

  // File browser / viewer
  let browserPath = $state('')
  let browserParent = $state(null)
  let dirEntries = $state([])
  let selectedFile = $state(null) // { path, text, reason }

  // Markdown files are rendered in the viewer with the same marked +
  // highlight.js pipeline as the chat, so both look identical.
  const isMarkdown = (path) => /\.md|\.markdown$/i.test(path ?? '')
  let toolsUsedInRun = $state(false)

  async function browse(path) {
    const resp = await apiGet(`/api/list?path=${encodeURIComponent(path)}`)
    browserPath = resp.path ?? ''
    browserParent = resp.parent ?? null
    dirEntries = resp.entries ?? []
  }

  // Link clicks inside rendered markdown previews. Relative file links
  // would otherwise resolve against the app page URL (e.g. /app.py) and
  // navigate the SPA away to an invalid endpoint; instead load them in
  // the viewer, resolved against the markdown file's own directory.
  // External links open in a new tab; in-page anchors scroll natively.
  function normalizePath(p) {
    const parts = []
    for (const seg of p.split('/')) {
      if (!seg || seg === '.') continue
      if (seg === '..') parts.pop()
      else parts.push(seg)
    }
    return parts.join('/')
  }

  async function openLinkedFile(path) {
    const f = await apiGet(`/api/file?path=${encodeURIComponent(path)}`)
    if (f.path && !f.error) {
      selectedFile = f
      // Sync the directory browser to the file's location (the selected
      // entry is highlighted via selectedFile.path).
      await browse(path.split('/').slice(0, -1).join('/'))
      return
    }
    // Not a file — maybe a directory link: navigate the browser there.
    const list = await apiGet(`/api/list?path=${encodeURIComponent(path)}`)
    if (list.entries) {
      selectedFile = null
      browserPath = list.path ?? ''
      browserParent = list.parent ?? null
      dirEntries = list.entries
    } else {
      entries.push({ role: 'system', text: `⚠️ link target not found: ${path}` })
    }
  }

  function onMdClick(e) {
    const a = e.target.closest('a')
    if (!a) return
    const href = a.getAttribute('href') ?? ''
    if (!href || href.startsWith('#')) return // in-page anchor: native scroll
    if (/^[a-z][a-z0-9+.-]*:/i.test(href) || href.startsWith('//')) {
      a.target = '_blank'
      a.rel = 'noopener noreferrer'
      return
    }
    e.preventDefault()
    const [pathPart] = href.split('#')
    if (!pathPart) return
    const base = selectedFile.path.split('/').slice(0, -1).join('/')
    openLinkedFile(normalizePath(base ? `${base}/${pathPart}` : pathPart))
  }

  async function selectEntry(e) {
    if (e.dir) {
      selectedFile = null
      await browse(e.path)
    } else {
      selectedFile = await apiGet(`/api/file?path=${encodeURIComponent(e.path)}`)
    }
  }

  // After a run that used tools, files may have changed: reload the current
  // directory and the open file. If either vanished in the meantime, fall
  // back to the project root.
  async function refreshFiles() {
    const list = await apiGet(`/api/list?path=${encodeURIComponent(browserPath)}`)
    if (!list.entries) {
      selectedFile = null
      await browse('')
      return
    }
    browserPath = list.path ?? ''
    browserParent = list.parent ?? null
    dirEntries = list.entries
    if (selectedFile) {
      const f = await apiGet(`/api/file?path=${encodeURIComponent(selectedFile.path)}`)
      if (f.error) {
        selectedFile = null
        await browse('')
      } else {
        selectedFile = f
      }
    }
  }

  function fmtSize(s) {
    if (s == null) return ''
    if (s < 1024) return `${s} B`
    if (s < 1024 * 1024) return `${(s / 1024).toFixed(1)} KB`
    return `${(s / 1024 / 1024).toFixed(1)} MB`
  }

  function renderMarkdown(text) {
    return marked.parse(text ?? '', { async: false })
  }

  // Auto-scroll only when the user is already at (near) the bottom:
  // scrolling up while the agent works stops the snapping, scrolling
  // back to the bottom re-enables it.
  let stickToBottom = true
  function onChatScroll() {
    if (!chatEl) return
    stickToBottom = chatEl.scrollHeight - chatEl.scrollTop - chatEl.clientHeight < 40
  }

  let scrollQueued = false
  async function scrollToBottom() {
    if (scrollQueued) return
    scrollQueued = true
    requestAnimationFrame(async () => {
      scrollQueued = false
      await tick()
      if (chatEl && stickToBottom) chatEl.scrollTop = chatEl.scrollHeight
    })
  }

  function currentAssistant() {
    return entries.findLast((e) => e.role === 'assistant') ?? null
  }

  async function apiGet(url) {
    try {
      const r = await fetch(url)
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return await r.json()
    } catch (err) {
      console.error('GET', url, err)
      return { success: false, error: err.message }
    }
  }

  async function apiPost(url, body) {
    try {
      const r = await fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body ?? {}),
      })
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return await r.json()
    } catch (err) {
      console.error('POST', url, err)
      return { success: false, error: err.message }
    }
  }

  async function refreshState() {
    const resp = await apiGet('/state')
    if (!resp.success) return
    currentModel = resp.data.model
    thinkingLevel = resp.data.thinkingLevel
    const levels = await apiGet('/thinking_levels')
    if (levels.success) thinkingLevels = levels.data.levels
  }

  async function refreshModels() {
    const resp = await apiGet('/models')
    if (resp.success) models = resp.data.models
  }

  async function refreshStats() {
    const resp = await apiGet('/stats')
    if (resp.success) stats = resp.data
  }

  // Debounced variant: collapses bursts of triggers (agent_end + agent_settled,
  // rapid message_end events) into a single /stats fetch.
  let statsTimer = null
  function refreshStatsDebounced() {
    if (statsTimer) return
    statsTimer = setTimeout(() => {
      statsTimer = null
      refreshStats()
    }, 300)
  }

  // Guard against out-of-order responses: only the latest request may
  // update the sessions list / current marker.
  let sessionsReq = 0
  async function refreshSessions() {
    const id = ++sessionsReq
    const resp = await apiGet('/sessions')
    if (id !== sessionsReq) return
    sessions = resp.sessions ?? []
    currentSessionPath = sessions.find((s) => s.current)?.path ?? ''
  }

  async function onModelChange(e) {
    const [provider, modelId] = e.target.value.split('::')
    const resp = await apiPost('/set_model', { provider, modelId })
    if (resp.success) {
      currentModel = resp.data
      const levels = await apiGet('/thinking_levels')
      if (levels.success) thinkingLevels = levels.data.levels
      if (!thinkingLevels.includes(thinkingLevel)) thinkingLevel = 'off'
    }
  }

  async function onThinkingChange(e) {
    const resp = await apiPost('/set_thinking', { level: e.target.value })
    if (resp.success) thinkingLevel = e.target.value
  }

  async function loadProjects() {
    const resp = await apiGet('/api/projects')
    projects = resp.projects ?? []
    currentProjectId = projects.find((p) => p.current)?.id ?? ''
  }

  // Full (re)initialization: after mount, a project switch, or a
  // project_switched event from another tab.
  async function reinit() {
    entries = []
    attachments = []
    stickToBottom = true // fresh content: start at the end
    selectedFile = null
    toolsUsedInRun = false
    await browse('')
    apiGet('/api/file?path=README.md').then((r) => {
      if (r.path && !r.error) selectedFile = r
    })
    await loadHistory()
    await Promise.all([
      refreshState(),
      refreshModels(),
      refreshStats(),
      refreshSessions(),
      loadCommands(),
      loadProjects(),
    ])
  }

  async function switchProject(id) {
    const resp = await apiPost(`/api/projects/${id}/open`)
    if (!resp.success) {
      entries.push({ role: 'system', text: `⚠️ ${resp.error ?? 'failed to open project'}` })
      await loadProjects() // reset the select to the real current project
      return
    }
    // Our SSE stream is still subscribed to the now-terminated old pi
    // process — reconnect, or we won't see any events from the new one.
    // (Other tabs learn via the project_switched event; the initiating
    // tab must reconnect itself.)
    reconnectEvents()
    await reinit()
    inputEl?.focus()
  }

  async function onProjectChange(e) {
    const id = e.target.value
    if (!id || id === currentProjectId) return
    if (streaming && !window.confirm('Switching projects restarts the agent and kills the running session. Continue?')) {
      e.target.value = currentProjectId
      return
    }
    await switchProject(id)
  }

  async function browseDirs(path) {
    const resp = await apiGet(`/api/dirs?path=${encodeURIComponent(path)}`)
    if (resp.error) return
    npBase = resp.base ?? ''
    npPath = resp.path ?? ''
    npParent = resp.parent ?? null
    npEntries = resp.entries ?? []
  }

  function toggleNewProject() {
    showNewProject = !showNewProject
    if (showNewProject) browseDirs('')
  }

  function toggleManage() {
    showManage = !showManage
  }

  // Detach = remove from the registry only; the directory stays on disk.
  async function detachProject(p) {
    if (p.current) return
    if (!window.confirm(`Detach project “${p.name}” from the list?\n${p.path}\n\nThe directory itself is not deleted.`)) return
    const resp = await apiPost(`/api/projects/${p.id}/detach`)
    if (!resp.success) {
      entries.push({ role: 'system', text: `⚠️ ${resp.error ?? 'failed to detach project'}` })
    }
    await loadProjects()
  }

  function npFullPath() {
    return npPath ? `${npBase}/${npPath}` : npBase
  }

  // Register `path` as a project (creating it first when it doesn't
  // exist) and switch to it.
  async function chooseProjectDir(path, gitInit) {
    const resp = await apiPost('/api/projects', { path, gitInit })
    if (!resp.success) {
      entries.push({ role: 'system', text: `⚠️ ${resp.error ?? 'failed to create project'}` })
      return
    }
    showNewProject = false
    npFolder = ''
    npGit = false
    await loadProjects()
    if (streaming && !window.confirm('Switch to the new project now? This restarts the agent and kills the running session.')) return
    await switchProject(resp.project.id)
  }

  async function onSessionChange(e) {
    const path = e.target.value
    if (!path || path === currentSessionPath) return
    const resp = await apiPost('/switch_session', { path })
    if (resp.success && !resp.data?.cancelled) {
      currentSessionPath = path
      entries = []
      await loadHistory()
      await refreshState()
      await refreshStats()
      inputEl?.focus()
    }
    await refreshSessions()
  }

  function handleEvent(ev) {
    switch (ev.type) {
      case 'agent_start':
        streaming = true
        break

      case 'message_start':
        if (ev.message?.role === 'assistant') {
          entries.push({ role: 'assistant', text: '', thinking: '' })
          scrollToBottom()
        }
        break

      case 'message_update': {
        const d = ev.assistantMessageEvent
        let a = currentAssistant()
        if (!a) {
          a = { role: 'assistant', text: '', thinking: '' }
          entries.push(a)
        }
        if (d?.type === 'text_delta') {
          a.text += d.delta
        } else if (d?.type === 'thinking_delta') {
          a.thinking += d.delta
        }
        scrollToBottom()
        break
      }

      case 'message_end':
        if (ev.message?.role === 'assistant') {
          const a = currentAssistant()
          const text = (ev.message.content ?? [])
            .filter((c) => c.type === 'text')
            .map((c) => c.text)
            .join('')
          const thinking = (ev.message.content ?? [])
            .filter((c) => c.type === 'thinking')
            .map((c) => c.thinking)
            .join('')
          if (a) {
            a.text = text
            if (thinking) a.thinking = thinking
          } else if (text || thinking) {
            entries.push({ role: 'assistant', text, thinking })
          }
          // Live token/cost update once per completed assistant message.
          refreshStatsDebounced()
          // Surface failed/aborted assistant turns
          if (ev.message.stopReason === 'error') {
            entries.push({
              role: 'system',
              text: `❌ ${ev.message.errorMessage ?? 'The assistant turn failed.'}`,
            })
          } else if (ev.message.stopReason === 'aborted') {
            entries.push({ role: 'system', text: '⏹ aborted' })
          }
        }
        scrollToBottom()
        break

      case 'auto_retry_start':
        entries.push({
          role: 'system',
          text: `⚠️ ${ev.errorMessage ?? 'transient error'} — retrying (attempt ${ev.attempt}/${ev.maxAttempts} in ${Math.round((ev.delayMs ?? 0) / 1000)}s)…`,
        })
        scrollToBottom()
        break

      case 'auto_retry_end':
        if (!ev.success) {
          entries.push({
            role: 'system',
            text: `❌ Retry failed after ${ev.attempt} attempts: ${ev.finalError ?? 'unknown error'}`,
          })
        }
        scrollToBottom()
        break

      case 'compaction_end':
        if (!ev.result && !ev.aborted) {
          entries.push({
            role: 'system',
            text: `❌ Compaction failed: ${ev.errorMessage ?? 'unknown error'}`,
          })
        }
        scrollToBottom()
        break

      case 'extension_error':
        entries.push({
          role: 'system',
          text: `❌ Extension error (${ev.extensionPath}): ${ev.error}`,
        })
        scrollToBottom()
        break

      case 'tool_execution_start':
        toolsUsedInRun = true
        entries.push({
          role: 'tool',
          name: ev.toolName,
          args: ev.args,
          output: '',
          done: false,
          isError: false,
        })
        scrollToBottom()
        break

      case 'tool_execution_update': {
        const t = entries.findLast((e) => e.role === 'tool' && !e.done)
        if (t) {
          t.output = (ev.partialResult?.content ?? [])
            .map((c) => c.text ?? '')
            .join('')
        }
        scrollToBottom()
        break
      }

      case 'tool_execution_end': {
        const t = entries.findLast((e) => e.role === 'tool' && !e.done)
        if (t) {
          t.output = (ev.result?.content ?? []).map((c) => c.text ?? '').join('')
          t.done = true
          t.isError = !!ev.isError
        }
        scrollToBottom()
        break
      }

      case 'agent_end':
      case 'agent_settled':
        streaming = false
        refreshStatsDebounced()
        refreshSessions() // pick up new session files / reordered mtimes
        if (toolsUsedInRun) {
          // Consumed on the first of agent_end/agent_settled; the second is a no-op.
          toolsUsedInRun = false
          refreshFiles()
        }
        scrollToBottom()
        // Ready for the next prompt without clicking.
        tick().then(() => inputEl?.focus())
        break

      case 'extension_ui_request':
        handleUiRequest(ev).catch((err) => console.error('UI request error', err))
        break

      case 'project_switched':
        // Another tab (or client) switched the active project: our SSE
        // stream is attached to the now-dead pi process, so reconnect
        // and load the new project's state.
        entries.push({
          role: 'system',
          text: `⇄ switched to project “${ev.project?.name ?? '?'}”`,
        })
        reconnectEvents()
        reinit()
        break
    }
  }

  async function handleUiRequest(req) {
    let resp = { type: 'extension_ui_response', id: req.id }
    if (req.method === 'notify') {
      // empty response for one-way notify
    } else if (req.method === 'setStatus' || req.method === 'setWidget' || req.method === 'setTitle') {
      if (req.method === 'setTitle' && req.title) document.title = req.title
    } else if (req.method === 'confirm') {
      resp.confirmed = window.confirm(`${req.title ?? ''}\n${req.message ?? ''}`)
    } else if (req.method === 'select') {
      const choice = window.prompt(
        `${req.title ?? 'Choose:'}\n` + req.options.map((o, i) => `${i + 1}. ${o}`).join('\n')
      )
      const idx = parseInt(choice, 10) - 1
      if (idx >= 0 && idx < req.options.length) resp.value = req.options[idx]
      else resp.cancelled = true
    } else if (req.method === 'input' || req.method === 'editor') {
      const v = window.prompt(req.title ?? 'Input:', req.prefill ?? '')
      if (v === null) resp.cancelled = true
      else resp.value = v
    } else {
      resp.cancelled = true
    }
    await apiPost('/ui-response', resp)
  }

  function onPickImages(e) {
    for (const file of e.target.files) {
      if (file.type.startsWith('image/')) {
        const reader = new FileReader()
        reader.onload = () => {
          const dataUrl = reader.result
          attachments.push({
            kind: 'image',
            name: file.name,
            data: dataUrl.split(',')[1],
            mimeType: file.type,
            url: dataUrl,
          })
        }
        reader.readAsDataURL(file)
      } else {
        // Text-ish files: inline content into the prompt message
        const reader = new FileReader()
        reader.onload = () => {
          attachments.push({
            kind: 'text',
            name: file.name,
            text: reader.result,
          })
        }
        reader.readAsText(file)
      }
    }
    e.target.value = ''
  }

  // Intercept slash commands: built-in TUI commands don't work via RPC, so map
  // the useful ones to their RPC equivalents, pass through commands pi itself
  // recognizes (extension/skill/template), and block the rest with a hint.
  async function handleSlashCommand(message) {
    const [cmd, ...rest] = message.slice(1).split(/\s+/)
    const arg = rest.join(' ')
    switch (cmd) {
      case 'new':
        await newSession()
        return
      case 'abort':
        await abort()
        return
      case 'compact': {
        const r = await apiPost('/compact', arg ? { customInstructions: arg } : {})
        entries.push({
          role: 'system',
          text: r.success
            ? ` compacted: ${r.data?.tokensBefore ?? '?'} → ~${r.data?.estimatedTokensAfter ?? '?'} tokens`
            : `⚠️ compact failed: ${r.error ?? 'unknown error'}`,
        })
        return
      }
      case 'name': {
        if (!arg) {
          entries.push({ role: 'system', text: '⚠️ usage: /name <session name>' })
          return
        }
        const r = await apiPost('/set_session_name', { name: arg })
        if (r.success) await refreshSessions()
        else entries.push({ role: 'system', text: `⚠️ ${r.error ?? 'failed to set name'}` })
        return
      }
      case 'export': {
        const r = await apiPost('/export_html')
        entries.push({
          role: 'system',
          text: r.success ? ` exported to ${r.data?.path ?? 'file'}` : `⚠️ export failed: ${r.error ?? 'unknown error'}`,
        })
        return
      }
      default:
        if (slashCommands.some((c) => c.name === cmd)) {
          // Recognized by pi (extension/skill/template) — forward as prompt.
          entries.push({ role: 'user', text: message })
          const body = { message }
          if (streaming) body.streamingBehavior = 'steer'
          await apiPost('/prompt', body)
        } else {
          const available = [
            ...BUILTIN_COMMANDS.map((c) => '/' + c.name),
            ...slashCommands.map((c) => '/' + c.name),
          ].join(', ')
          entries.push({
            role: 'system',
            text: `⚠️ Unknown command /${cmd}. Built-in TUI commands don't work here. Available: ${available}`,
          })
        }
    }
  }

  async function sendPrompt() {
    const trimmed = input.trim()
    if (trimmed.startsWith('/')) {
      input = ''
      await handleSlashCommand(trimmed)
      scrollToBottom()
      return
    }
    const textAttachments = attachments.filter((a) => a.kind === 'text')
    const imageAttachments = attachments.filter((a) => a.kind === 'image')
    let message = input.trim()
    for (const t of textAttachments) {
      message += `${message ? '\n\n' : ''}Content of \`${t.name}\`:\n\`\`\`\n${t.text}\n\`\`\``
    }
    if (!message && imageAttachments.length === 0) return
    const images = imageAttachments.map(({ data, mimeType }) => ({ data, mimeType }))
    entries.push({
      role: 'user',
      text: message,
      images: imageAttachments.map((a) => a.url),
    })
    input = ''
    attachments = []
    stickToBottom = true // sending is an explicit jump to the end
    scrollToBottom()
    const body = { message }
    if (images.length) body.images = images
    if (streaming) body.streamingBehavior = 'steer'
    const data = await apiPost('/prompt', body)
    if (!data.success) {
      entries.push({ role: 'assistant', text: `⚠️ ${data.error ?? 'prompt rejected'}` })
    }
  }

  async function abort() {
    await apiPost('/abort')
  }

  async function newSession() {
    await apiPost('/new_session')
    entries = []
    await refreshSessions()
    await refreshStats()
    inputEl?.focus()
  }

  async function loadHistory() {
    const resp = await apiGet('/messages')
    if (!resp.success) return
    for (const m of resp.data.messages) {
      if (m.role === 'user') {
        const parts = typeof m.content === 'string' ? [{ type: 'text', text: m.content }] : (m.content ?? [])
        const text = parts.filter((c) => c.type === 'text').map((c) => c.text).join('')
        const images = parts
          .filter((c) => c.type === 'image')
          .map((c) => `data:${c.mimeType ?? c.source?.mediaType ?? 'image/png'};base64,${c.data ?? c.source?.data}`)
        if (text || images.length) entries.push({ role: 'user', text, images })
      } else if (m.role === 'assistant') {
        const text = (m.content ?? []).filter((c) => c.type === 'text').map((c) => c.text).join('')
        const thinking = (m.content ?? []).filter((c) => c.type === 'thinking').map((c) => c.thinking).join('')
        if (text || thinking) entries.push({ role: 'assistant', text, thinking })
      } else if (m.role === 'toolResult') {
        entries.push({
          role: 'tool',
          name: m.toolName,
          args: {},
          output: (m.content ?? []).map((c) => c.text ?? '').join(''),
          done: true,
          isError: !!m.isError,
        })
      }
    }
    scrollToBottom()
  }

  async function loadCommands() {
    const r = await apiGet('/commands')
    if (r.success) slashCommands = r.data?.commands ?? []
  }

  let eventSource = null
  function reconnectEvents() {
    eventSource?.close()
    const es = new EventSource('/events')
    eventSource = es
    // Retry on (re)connect: covers page loads while the server was down
    // and picks up new extensions/prompts after a server restart.
    es.onopen = () => loadCommands()
    es.onmessage = (e) => {
      try {
        handleEvent(JSON.parse(e.data))
      } catch (err) {
        console.error('event handler error', err)
      }
    }
    es.onerror = (e) => console.error('SSE error', e)
  }

  onMount(() => {
    reinit() // also shows the project README in the file viewer, if present
    reconnectEvents()
    inputEl?.focus()
    return () => eventSource?.close()
  })

  function applySlashSuggestion(c) {
    input = '/' + c.name + ' '
  }

  // Auto-grow the prompt textarea with the typed lines, up to
  // INPUT_MAX_LINES; beyond that it scrolls instead of growing.
  const INPUT_MAX_LINES = 8
  function autogrowInput() {
    if (!inputEl) return
    inputEl.style.height = 'auto'
    const style = getComputedStyle(inputEl)
    const line = parseFloat(style.lineHeight) || 20
    const pad = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom)
    // The textarea is content-box: style.height sets the content height,
    // but scrollHeight includes the padding — subtract it, otherwise the
    // padding is counted twice and the field is always one line too tall.
    const content = inputEl.scrollHeight - pad
    inputEl.style.height = Math.min(content, line * INPUT_MAX_LINES) + 'px'
    inputEl.style.overflowY = content > line * INPUT_MAX_LINES ? 'auto' : 'hidden'
  }
  $effect(() => {
    input // track: re-run on every keystroke and on reset after send
    autogrowInput()
  })

  function onKeydown(e) {
    if (slashSuggestions.length) {
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        slashIndex = (slashIndex + 1) % slashSuggestions.length
        return
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        slashIndex = (slashIndex - 1 + slashSuggestions.length) % slashSuggestions.length
        return
      }
      // Tab completes without sending; Enter picks the selection.
      if (e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) {
        e.preventDefault()
        applySlashSuggestion(slashSuggestions[slashIndex])
        return
      }
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      sendPrompt()
    }
  }

  function fmtCost(c) {
    return c == null ? '—' : `$${c.toFixed(2)}`
  }
</script>

<main>
  <div class="body">
   <div class="chatcol">
    <div class="toolbar">
      <div class="tgroup">
      <select bind:value={theme} title="Theme">
        <option value="light">light</option>
        <option value="claude">claude</option>
        <option value="dark">dark</option>
      </select>
      </div>
      <div class="tgroup">
      <select value={currentProjectId} onchange={onProjectChange} title="Project">
        {#each projects as p (p.id)}
          <option value={p.id}>{p.name}</option>
        {/each}
      </select>
      <button onclick={toggleNewProject} title="New project">add</button>
      <button onclick={toggleManage} title="Manage projects">manage</button>
      </div>
      <div class="tgroup">
      <select value={currentModel ? `${currentModel.provider}::${currentModel.id}` : ''} onchange={onModelChange}>
        {#each models as m}
          <option value={`${m.provider}::${m.id}`} selected={currentModel && m.provider === currentModel.provider && m.id === currentModel.id}>
            {m.name ?? m.id}
          </option>
        {/each}
      </select>
      <select value={thinkingLevel} onchange={onThinkingChange}>
        {#each thinkingLevels as l}
          <option value={l} selected={l === thinkingLevel}>{l}</option>
        {/each}
      </select>
      </div>
      <div class="tgroup">
      <select class="sessionselect" value={currentSessionPath} onchange={onSessionChange}>
        {#if !currentSessionPath}
          <option value="" disabled>sessions…</option>
        {/if}
        {#each sessions as s (s.path)}
          <option value={s.path}>
            {s.name}{s.current ? ' (current)' : ''} — {new Date(s.mtime * 1000).toLocaleString()}
          </option>
        {/each}
      </select>
      <button onclick={newSession}>new</button>
      </div>
    </div>
  {#if showNewProject}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="np-overlay" role="presentation" onclick={(e) => e.target === e.currentTarget && (showNewProject = false)}>
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <div class="np-modal" role="dialog" aria-modal="true" aria-label="Choose project directory" tabindex="-1" oninput={pokeDialogTimer} onfocusin={pokeDialogTimer} onclick={pokeDialogTimer}>
        <div class="np-head">
          <span class="np-title">Choose project directory</span>
          <button class="np-close" onclick={() => (showNewProject = false)}>×</button>
        </div>
        <div class="np-path">
          <span title={npFullPath()}>{npBase}/{npPath}</span>
        </div>
        <div class="np-list">
          {#if npParent !== null}
            <button class="direntry" onclick={() => browseDirs(npParent)}>
              <span class="icon">📁</span>
              <span class="name">..</span>
            </button>
          {/if}
          {#each npEntries as e}
            <button class="direntry" onclick={() => browseDirs(e.path)}>
              <span class="icon">📁</span>
              <span class="name">{e.name}</span>
            </button>
          {:else}
            <div class="np-empty">No subdirectories here.</div>
          {/each}
        </div>
        <div class="np-actions">
          <input
            type="text"
            bind:value={npFolder}
            placeholder="new folder name"
            onkeydown={(e) => e.key === 'Enter' && npFolder.trim() && chooseProjectDir(`${npFullPath()}/${npFolder.trim()}`, npGit)}
          />
          <label><input type="checkbox" bind:checked={npGit} /> git init</label>
          <button onclick={() => chooseProjectDir(`${npFullPath()}/${npFolder.trim()}`, npGit)} disabled={!npFolder.trim()}>Create</button>
          <button onclick={() => chooseProjectDir(npFullPath(), false)} disabled={!npPath}>Select</button>
        </div>
      </div>
    </div>
  {/if}
  {#if showManage}
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="np-overlay" role="presentation" onclick={(e) => e.target === e.currentTarget && (showManage = false)}>
      <!-- svelte-ignore a11y_click_events_have_key_events -->
      <div class="np-modal" role="dialog" aria-modal="true" aria-label="Manage projects" tabindex="-1" oninput={pokeDialogTimer} onfocusin={pokeDialogTimer} onclick={pokeDialogTimer}>
        <div class="np-head">
          <span class="np-title">Manage projects</span>
          <button class="np-close" onclick={() => (showManage = false)}>×</button>
        </div>
        <div class="np-list">
          {#each projects as p (p.id)}
            <div class="mp-row">
              <span class="mp-name">{p.name}{p.current ? ' (current)' : ''}</span>
              <span class="mp-path" title={p.path}>{p.path}</span>
              <button
                onclick={() => detachProject(p)}
                disabled={p.current}
                title={p.current ? 'Switch to another project first' : 'Detach (the directory stays on disk)'}
              >detach</button>
            </div>
          {:else}
            <div class="np-empty">No projects registered.</div>
          {/each}
        </div>
      </div>
    </div>
  {/if}
  <div class="chat" bind:this={chatEl} onscroll={onChatScroll}>
    {#if entries.length === 0}
      <div class="chat-empty" aria-hidden="true">
        <svg class="logo" viewBox="0 0 120 120" xmlns="http://www.w3.org/2000/svg">
          <!-- circuit traces -->
          <g stroke="currentColor" stroke-width="1.5" fill="none" opacity="0.5">
            <path d="M60 18 V34" />
            <path d="M22 44 L36 52" />
            <path d="M98 44 L84 52" />
            <path d="M34 96 L44 84" />
            <path d="M86 96 L76 84" />
          </g>
          <g fill="currentColor" opacity="0.5">
            <circle cx="60" cy="14" r="4" />
            <circle cx="18" cy="42" r="4" />
            <circle cx="102" cy="42" r="4" />
            <circle cx="31" cy="101" r="4" />
            <circle cx="89" cy="101" r="4" />
          </g>
          <!-- pi glyph -->
          <path
            d="M34 44 h52 v8 h-10 v22 c0 6 3 8 8 8 v8 c-10 0 -16 -5 -16 -14 v-24 h-8 v26 c0 6 -2 10 -8 12 l-4 -7 c3 -1 4 -3 4 -7 v-24 h-8 z"
            fill="currentColor"
          />
        </svg>
        <div class="chat-empty-text">pi agent web UI</div>
      </div>
    {/if}
    {#each entries as entry}
      {#if entry.role === 'system'}
        <div class="msg system">{entry.text}</div>
      {:else if entry.role === 'user'}
        <div class="msg user">
          {entry.text}
          {#if entry.images?.length}
            <div class="imgs">
              {#each entry.images as src}
                <img {src} alt="attachment" />
              {/each}
            </div>
          {/if}
        </div>
      {:else if entry.role === 'assistant'}
        <div class="msg assistant md">
          {#if entry.thinking}
            <details class="thinking-block" open>
              <summary>thinking</summary>
              <pre>{entry.thinking}</pre>
            </details>
          {/if}
          {@html renderMarkdown(entry.text)}
        </div>
      {:else}
        <details class="msg tool" class:error={entry.isError} class:ok={entry.done && !entry.isError} open>
          <summary>
            🔧 {entry.name}
            {#if entry.args?.command}<code>{entry.args.command}</code>{/if}
            {#if !entry.done}<span class="spinner">…</span>{/if}
            {#if entry.isError}❌{/if}
          </summary>
          {#if entry.output}<pre>{entry.output}</pre>{/if}
        </details>
      {/if}
    {/each}
    {#if streaming && !currentAssistant()}
      <div class="msg assistant thinking">thinking…</div>
    {/if}
  </div>

  {#if attachments.length}
    <div class="attachments">
      {#each attachments as a, i}
        <div class="thumb">
          {#if a.kind === 'image'}
            <img src={a.url} alt="attachment" />
          {:else}
            <div class="textfile">📄 {a.name}</div>
          {/if}
          <button onclick={() => attachments.splice(i, 1)}>×</button>
        </div>
      {/each}
    </div>
  {/if}

  {#if streaming}
    <div class="progress-bar" aria-hidden="true"></div>
  {/if}
  {#if slashSuggestions.length}
    <div class="slash-menu">
      {#each slashSuggestions as c, i (c.name)}
        <button class="slash-item" class:selected={i === slashIndex} onmousedown={(e) => { e.preventDefault(); applySlashSuggestion(c) }}>
          <span class="slash-name">/{c.name}</span>
          {#if c.description}<span class="slash-desc">{c.description}</span>{/if}
        </button>
      {/each}
    </div>
  {/if}
  <footer>
    <label class="attach">
      📎
      <input type="file" multiple onchange={onPickImages} hidden />
    </label>
    <textarea
      bind:this={inputEl}
      bind:value={input}
      onfocus={() => { showNewProject = false; showManage = false }}
      onkeydown={onKeydown}
      placeholder={streaming ? 'Agent is working — type to steer, Enter to send…' : 'Prompt pi… (Enter to send, Shift+Enter for newline)'}
      rows="2"
    ></textarea>
    <!-- While the agent works, an empty input means the button aborts;
         with content it becomes a steer/send button again -->
    {#if streaming && !input.trim() && attachments.length === 0}
      <button onclick={abort}>abort</button>
    {:else}
      <button onclick={sendPrompt} disabled={!input.trim() && attachments.length === 0}>send</button>
    {/if}
  </footer>

  <div class="statusbar">
    {#if stats}
      <span>tokens: {stats.tokens?.total?.toLocaleString() ?? '—'}</span>
      <span>cost: {fmtCost(stats.cost)}</span>
      {#if stats.contextUsage}
        <span>context: {stats.contextUsage.percent != null ? Math.round(stats.contextUsage.percent * 100) / 100 : '—'}% ({stats.contextUsage.tokens?.toLocaleString() ?? '—'}/{stats.contextUsage.contextWindow?.toLocaleString() ?? '—'})</span>
      {/if}
      <span>tool calls: {stats.toolCalls ?? 0}</span>
    {:else}
      <span>loading stats…</span>
    {/if}
  </div>
   </div>

   <aside>
    <div class="browser">
      <div class="browser-path">
        <span>/{browserPath}</span>
      </div>
      <div class="dirlist">
        {#if browserParent !== null}
          <button class="direntry" onclick={() => browse(browserParent)}>
            <span class="icon">📁</span>
            <span class="name">..</span>
          </button>
        {/if}
        {#each dirEntries as e}
          <button class="direntry" class:selected={selectedFile?.path === e.path} onclick={() => selectEntry(e)}>
            <span class="icon">{e.dir ? '📁' : '📄'}</span>
            <span class="name">{e.name}</span>
            <span class="size">{fmtSize(e.size)}</span>
          </button>
        {/each}
      </div>
    </div>
    <div class="viewer">
      {#if selectedFile}
        <div class="viewer-head">
          <span class="fname">{selectedFile.path}</span>
          <a class="dl" href={`/download/${selectedFile.path}`} download>download</a>
        </div>
        {#if selectedFile.text !== null && isMarkdown(selectedFile.path)}
          <!-- svelte-ignore a11y_click_events_have_key_events -->
          <!-- svelte-ignore a11y_no_static_element_interactions -->
          <div class="filecontent md" onclick={onMdClick}>{@html renderMarkdown(selectedFile.text)}</div>
        {:else if selectedFile.text !== null}
          <pre class="filecontent hljs"><code>{@html highlight(selectedFile.text, selectedFile.path)}</code></pre>
        {:else}
          <div class="filecontent binary">No preview ({selectedFile.reason}) — use download.</div>
        {/if}
      {:else}
        <div class="filecontent placeholder">Select a file to preview it here.</div>
      {/if}
    </div>
   </aside>
  </div>
</main>
