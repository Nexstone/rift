/**
 * AI-powered backtest analysis using Claude API.
 */

import Anthropic from '@anthropic-ai/sdk'
import * as fs from 'node:fs'
import * as path from 'node:path'

const CONFIG_PATH = path.join(process.env.HOME || '~', '.rift', 'config.json')

interface AnalyzerConfig {
  apiKey: string
  model: string
}

function loadAIConfig(): AnalyzerConfig | null {
  // Check env var first
  const envKey = process.env.RIFT_AI_API_KEY || process.env.ANTHROPIC_API_KEY
  if (envKey) {
    return {apiKey: envKey, model: 'claude-sonnet-4-20250514'}
  }

  // Check config file
  if (fs.existsSync(CONFIG_PATH)) {
    try {
      const config = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf-8'))
      const ai = config.ai
      if (ai?.api_key) {
        return {apiKey: ai.api_key, model: ai.model || 'claude-sonnet-4-20250514'}
      }
    } catch {}
  }

  return null
}

const SYSTEM_PROMPT = `You are a senior quantitative analyst reviewing backtest results for a trading strategy on Hyperliquid perpetual futures. Your job is to give a concise, actionable assessment.

Be direct and specific. No fluff. Focus on:
1. Is this strategy viable for real trading? Why or why not?
2. What are the critical risk issues?
3. What specific parameter changes would improve it?
4. What's the one thing the trader should fix first?

Keep your response under 200 words. Use plain language. If the strategy would blow up a real account, say so clearly.`

export async function analyzeBacktest(resultData: Record<string, unknown>): Promise<string> {
  const config = loadAIConfig()
  if (!config) {
    return 'No AI API key configured. Run: rift config set ai.api_key <your-anthropic-key>\nOr set RIFT_AI_API_KEY or ANTHROPIC_API_KEY environment variable.'
  }

  const client = new Anthropic({apiKey: config.apiKey})

  // Build a clean summary for the LLM
  const {chart, export: _, type, command, ...metrics} = resultData
  const trades = (resultData.export as any)?.trades || []

  const prompt = `Analyze this backtest result:

${JSON.stringify(metrics, null, 2)}

Number of trades in detail: ${trades.length}
${trades.length > 0 ? `First trade: ${JSON.stringify(trades[0])}
Last trade: ${JSON.stringify(trades[trades.length - 1])}` : 'No trades executed.'}

${trades.length === 0 ? 'The strategy made zero trades. This likely means the entry conditions were never met for this pair/timeframe combination.' : ''}`

  const response = await client.messages.create({
    model: config.model,
    max_tokens: 500,
    system: SYSTEM_PROMPT,
    messages: [{role: 'user', content: prompt}],
  })

  const textBlock = response.content.find(b => b.type === 'text')
  return textBlock ? textBlock.text : 'No analysis returned.'
}
