export const art = {
  parse: `<svg viewBox="0 0 320 160" font-family="Inter"><g font-size="9" fill="#6f6f6a" font-weight="600">
   <text x="6" y="12">SOURCE</text><text x="106" y="12">TOKENS</text><text x="216" y="12">PARSE TREE</text></g>
   <rect x="6" y="22" width="82" height="34" rx="5" fill="#f1f1ed" stroke="#e3e3de"/>
   <text x="16" y="43" font-size="12" font-family="JetBrains Mono" fill="#151515">x = a + b*c</text>
   <g fill="#edf1fc" stroke="#315fda" stroke-width="1">
   <rect x="106" y="22" width="26" height="16" rx="3"/><rect x="106" y="44" width="26" height="16" rx="3"/>
   <rect x="106" y="66" width="26" height="16" rx="3"/><rect x="106" y="88" width="26" height="16" rx="3"/>
   <rect x="106" y="110" width="26" height="16" rx="3"/></g>
   <g font-size="9" fill="#151515" text-anchor="middle" font-family="JetBrains Mono">
   <text x="119" y="33">x</text><text x="119" y="55">=</text><text x="119" y="77">a</text><text x="119" y="99">+</text><text x="119" y="121">b</text></g>
   <g stroke="#d2d2cc" stroke-width="1.2" fill="none"><path d="M262 40v14M262 54h-28v12M262 54h28v12M290 78v12M290 90h-14v12M290 90h14v12"/></g>
   <g fill="#fff" stroke="#151515" stroke-width="1.3">
   <circle cx="262" cy="34" r="10"/><circle cx="234" cy="72" r="10"/><circle cx="290" cy="72" r="10"/>
   <circle cx="276" cy="108" r="10"/><circle cx="304" cy="108" r="10"/></g>
   <g font-size="10" text-anchor="middle" fill="#151515" font-family="JetBrains Mono">
   <text x="262" y="38">=</text><text x="234" y="76">x</text><text x="290" y="76">+</text><text x="276" y="112">a</text><text x="304" y="112">*</text></g>
   <path d="M94 42h6M94 42l-4-3M94 42l-4 3M140 60h6" stroke="#9a9a94" stroke-width="1.2" fill="none"/>
   <path d="M92 40h8M144 62h56" stroke="#d2d2cc" stroke-width="1" stroke-dasharray="3 3"/></svg>`,

  moon: `<svg viewBox="0 0 320 160"><ellipse cx="160" cy="82" rx="118" ry="52" fill="none" stroke="#d2d2cc" stroke-width="1.2" stroke-dasharray="5 5"/>
   <circle cx="160" cy="82" r="34" fill="#edf1fc" stroke="#151515" stroke-width="1.8"/>
   <path d="M133 68c14 4 26-2 40 2s22-2 34 2M132 92c16-4 28 4 42 0s24 4 36 0" stroke="#315fda" stroke-width="1.1" fill="none" opacity=".7"/>
   <circle cx="278" cy="82" r="11" fill="#fff" stroke="#151515" stroke-width="1.6"/>
   <circle cx="275" cy="79" r="2.4" fill="#e3e3de"/><circle cx="281" cy="86" r="1.6" fill="#e3e3de"/>
   <g stroke="#151515" stroke-width="1.6" fill="none" stroke-linecap="round"><path d="M266 82h-20M252 77l-6 5 6 5"/><path d="M278 65v-16M273 55l5-6 5 6"/></g>
   <text x="240" y="46" font-size="10" font-family="Inter" font-weight="600" fill="#151515" text-anchor="middle">velocity</text>
   <text x="222" y="98" font-size="10" font-family="Inter" font-weight="600" fill="#151515" text-anchor="middle">gravity</text>
   <text x="160" y="86" font-size="11" font-family="Inter" font-weight="700" fill="#151515" text-anchor="middle">Earth</text></svg>`,

  nn: `<svg viewBox="0 0 320 160"><g stroke="#e3e3de" stroke-width=".9">
   <path d="M76 44 168 36M76 44 168 80M76 44 168 124M76 82 168 36M76 82 168 80M76 82 168 124M76 120 168 36M76 120 168 80M76 120 168 124"/>
   <path d="M168 36 254 60M168 36 254 104M168 80 254 60M168 80 254 104M168 124 254 60M168 124 254 104" stroke="#315fda" opacity=".45"/></g>
   <g fill="#fff" stroke="#151515" stroke-width="1.6">
   <circle cx="76" cy="44" r="11"/><circle cx="76" cy="82" r="11"/><circle cx="76" cy="120" r="11"/>
   <circle cx="168" cy="36" r="11" fill="#edf1fc"/><circle cx="168" cy="80" r="11" fill="#edf1fc"/><circle cx="168" cy="124" r="11" fill="#edf1fc"/>
   <circle cx="254" cy="60" r="11"/><circle cx="254" cy="104" r="11"/></g>
   <g font-size="9" font-family="Inter" fill="#9a9a94" text-anchor="middle" font-weight="600">
   <text x="76" y="146">input</text><text x="168" y="152">hidden</text><text x="254" y="132">output</text></g></svg>`,
} as const

export type ArtKey = keyof typeof art
