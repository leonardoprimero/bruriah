import * as vscode from 'vscode';
import { execFile } from 'child_process';
import { promisify } from 'util';

const execFileAsync = promisify(execFile);

interface LineLens {
  line_start: number;
  line_end: number;
  commit_sha: string;
  author: string;
  date: string;
  decision_title?: string;
  decision_ref?: string;
  status: 'active' | 'superseded' | 'deprecated' | 'amended' | 'unindexed';
  alert_relation?: string;
  successor_title?: string;
  successor_sha?: string;
  generations?: number;
}

interface FileLensResult {
  file_path: string;
  total_lines: number;
  indexed_decisions_count: number;
  stale_decisions_count: number;
  lenses: LineLens[];
}

function getExecutablePath(): string {
  const config = vscode.workspace.getConfiguration('bruriah');
  return config.get<string>('executablePath', 'bruriah');
}

/**
 * Executes `bruriah lens <file> --json` to get all decision annotations for the file in one pass.
 */
async function fetchFileLens(document: vscode.TextDocument): Promise<FileLensResult | null> {
  const exe = getExecutablePath();
  const filePath = document.uri.fsPath;
  const workspaceFolder = vscode.workspace.getWorkspaceFolder(document.uri);
  const cwd = workspaceFolder ? workspaceFolder.uri.fsPath : undefined;

  try {
    const { stdout } = await execFileAsync(exe, ['lens', filePath, '--json'], { cwd });
    return JSON.parse(stdout) as FileLensResult;
  } catch {
    return null;
  }
}

/**
 * Executes `bruriah why <target> --json` to get deep causal resolution for hover/details.
 */
async function fetchWhyTarget(filePath: string, line: number, cwd?: string): Promise<any | null> {
  const exe = getExecutablePath();
  const target = `${filePath}:${line}`;

  try {
    const { stdout } = await execFileAsync(exe, ['why', target, '--json'], { cwd });
    return JSON.parse(stdout);
  } catch {
    return null;
  }
}

class BruriahCodeLensProvider implements vscode.CodeLensProvider {
  private onDidChangeCodeLensesEmitter = new vscode.EventEmitter<void>();
  public readonly onDidChangeCodeLenses = this.onDidChangeCodeLensesEmitter.event;

  public refresh(): void {
    this.onDidChangeCodeLensesEmitter.fire();
  }

  async provideCodeLenses(
    document: vscode.TextDocument,
    _token: vscode.CancellationToken
  ): Promise<vscode.CodeLens[]> {
    const config = vscode.workspace.getConfiguration('bruriah');
    if (!config.get<boolean>('enableCodeLens', true)) {
      return [];
    }

    const lensResult = await fetchFileLens(document);
    if (!lensResult || !lensResult.lenses) {
      return [];
    }

    const codeLenses: vscode.CodeLens[] = [];

    for (const lens of lensResult.lenses) {
      if (lens.status === 'unindexed' || !lens.decision_title) {
        continue;
      }

      // Convert 1-based line to 0-based VS Code line
      const lineIndex = Math.max(0, lens.line_start - 1);
      const range = new vscode.Range(lineIndex, 0, lineIndex, 0);

      let title: string;
      if (lens.status === 'active') {
        title = `🏛️ Decisión: ${lens.decision_title} (${lens.commit_sha}) · [Ver por qué se decidió]`;
      } else {
        const rel = (lens.alert_relation || lens.status).toUpperCase();
        title = `🏛️ Decisión: ${lens.decision_title} (${lens.commit_sha}) · ⚠️ ${rel} por ${lens.successor_title || lens.successor_sha} · [Ver detalles]`;
      }

      codeLenses.push(
        new vscode.CodeLens(range, {
          title,
          command: 'bruriah.whyLine',
          arguments: [document.uri.fsPath, lens.line_start],
          tooltip: 'Inspeccionar razonamiento arquitectónico y linaje causal',
        })
      );
    }

    return codeLenses;
  }
}

class BruriahHoverProvider implements vscode.HoverProvider {
  async provideHover(
    document: vscode.TextDocument,
    position: vscode.Position,
    _token: vscode.CancellationToken
  ): Promise<vscode.Hover | null> {
    const config = vscode.workspace.getConfiguration('bruriah');
    if (!config.get<boolean>('enableHover', true)) {
      return null;
    }

    const workspaceFolder = vscode.workspace.getWorkspaceFolder(document.uri);
    const cwd = workspaceFolder ? workspaceFolder.uri.fsPath : undefined;
    const lineNumber = position.line + 1;

    const data = await fetchWhyTarget(document.uri.fsPath, lineNumber, cwd);
    if (!data || !data.governing_decision) {
      return null;
    }

    const dec = data.governing_decision;
    const lines: string[] = [];

    lines.push(`### 🏛️ Bruriah Architectural Archaeology`);
    lines.push(`**Decisión:** ${dec.subject}`);
    lines.push(`**Commit:** \`${dec.commit_sha?.slice(0, 8)}\` · **Autor:** ${dec.author} · **Fecha:** ${dec.date}`);

    if (data.lineage_alerts && data.lineage_alerts.length > 0) {
      lines.push('');
      for (const alert of data.lineage_alerts) {
        const rel = alert.relation.toUpperCase();
        lines.push(`> ⚠️ **ARCHITECTURAL DRIFT**: Esta decisión fue **${rel}** por:`);
        lines.push(`> **${alert.successor_subject || 'Sucesor'}** (\`${alert.successor_commit?.slice(0, 8)}\`)`);
        if (alert.depth > 1) {
          lines.push(`> ↳ Evolucionó a través de **${alert.depth} generaciones**.`);
        }
      }
    }

    if (dec.body) {
      lines.push('');
      lines.push('---');
      lines.push('**Razonamiento:**');
      lines.push(dec.body.trim().slice(0, 300) + (dec.body.length > 300 ? '…' : ''));
    }

    const md = new vscode.MarkdownString(lines.join('\n'));
    md.isTrusted = true;
    return new vscode.Hover(md);
  }
}

export function activate(context: vscode.ExtensionContext) {
  const codeLensProvider = new BruriahCodeLensProvider();

  // Register CodeLens & Hover for all languages
  context.subscriptions.push(
    vscode.languages.registerCodeLensProvider({ scheme: 'file' }, codeLensProvider),
    vscode.languages.registerHoverProvider({ scheme: 'file' }, new BruriahHoverProvider())
  );

  // Command: Explain why line exists
  context.subscriptions.push(
    vscode.commands.registerCommand('bruriah.whyLine', async (filePath?: string, line?: number) => {
      let targetFile = filePath;
      let targetLine = line;

      if (!targetFile) {
        const activeEditor = vscode.window.activeTextEditor;
        if (!activeEditor) {
          vscode.window.showWarningMessage('No hay ningún archivo activo.');
          return;
        }
        targetFile = activeEditor.document.uri.fsPath;
        targetLine = activeEditor.selection.active.line + 1;
      }

      const workspaceFolder = vscode.workspace.getWorkspaceFolder(vscode.Uri.file(targetFile));
      const cwd = workspaceFolder ? workspaceFolder.uri.fsPath : undefined;

      const data = await fetchWhyTarget(targetFile, targetLine || 1, cwd);
      if (!data) {
        vscode.window.showInformationMessage(`No se encontró decisión arquitectónica para ${targetFile}:${targetLine}`);
        return;
      }

      // Create Webview panel to display full reasoning
      const panel = vscode.window.createWebviewPanel(
        'bruriahWhy',
        `🏛️ Bruriah: ${data.governing_decision?.subject || 'Causal Resolution'}`,
        vscode.ViewColumn.Beside,
        { enableScripts: true }
      );

      const dec = data.governing_decision;
      const alerts = data.lineage_alerts || [];
      const alertsHtml = alerts.map((a: any) => `
        <div style="background:#4a1515;border-left:4px solid #da3633;padding:12px;margin:12px 0;border-radius:4px;">
          <strong>⚠️ ARCHITECTURAL DRIFT</strong><br>
          Esta decisión fue <strong>${a.relation.toUpperCase()}</strong> por 
          <em>${a.successor_subject || 'Sucesor'}</em> (<code>${a.successor_commit?.slice(0, 8)}</code>).
          ${a.depth > 1 ? `<br><small>Evolución: ${a.depth} generaciones</small>` : ''}
        </div>
      `).join('');

      panel.webview.html = `<!DOCTYPE html>
      <html>
      <head>
        <meta charset="utf-8">
        <style>
          body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; padding: 20px; line-height: 1.6; color: var(--vscode-foreground); background: var(--vscode-editor-background); }
          h1 { font-size: 20px; margin-bottom: 8px; color: var(--vscode-textLink-foreground); }
          .meta { font-size: 13px; color: var(--vscode-descriptionForeground); margin-bottom: 16px; }
          .body { white-space: pre-wrap; font-size: 14px; background: var(--vscode-textCodeBlock-background); padding: 16px; border-radius: 6px; }
          code { font-family: monospace; }
        </style>
      </head>
      <body>
        <h1>🏛️ ${dec?.subject || 'Sin decisión asociada'}</h1>
        <div class="meta">
          <strong>Commit:</strong> <code>${dec?.commit_sha || data.line_commit?.sha}</code> · 
          <strong>Autor:</strong> ${dec?.author || data.line_commit?.author} · 
          <strong>Fecha:</strong> ${dec?.date || data.line_commit?.date}
        </div>
        ${alertsHtml}
        <h3>Razonamiento y contexto original:</h3>
        <div class="body">${dec?.body || 'No se registraron notas de decisión para este commit.'}</div>
      </body>
      </html>`;
    })
  );

  // Command: Open Visual DAG Explorer
  context.subscriptions.push(
    vscode.commands.registerCommand('bruriah.openUI', async () => {
      const term = vscode.window.createTerminal('Bruriah UI');
      term.sendText(`${getExecutablePath()} ui`);
      term.show();
    })
  );
}

export function deactivate() {}
