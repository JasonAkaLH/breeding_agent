import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MCPSettingsPanel } from './MCPSettingsPanel';
import { ApiError } from '../api/client';
import type { ApiClient } from '../api/client';

type SettingsApi = Pick<ApiClient,
  | 'listMCPServers'
  | 'createMCPServer'
  | 'patchMCPServer'
  | 'testMCPServer'
  | 'deleteMCPServer'
  | 'listMCPGrants'
  | 'deleteMCPGrant'
  | 'clearMCPServerGrants'
>;

function emptySettingsApi(overrides: Partial<SettingsApi> = {}): SettingsApi {
  return {
    listMCPServers: vi.fn(async () => ({ servers: [] })),
    createMCPServer: vi.fn(),
    patchMCPServer: vi.fn(),
    testMCPServer: vi.fn(),
    deleteMCPServer: vi.fn(),
    listMCPGrants: vi.fn(async () => ({ grants: [] })),
    deleteMCPGrant: vi.fn(),
    clearMCPServerGrants: vi.fn(),
    ...overrides,
  } as SettingsApi;
}

async function openCreateForm() {
  fireEvent.click(await screen.findByRole('button', { name: '添加 MCP 服务' }));
  await screen.findByRole('dialog', { name: '添加 MCP 服务' });
}

describe('MCPSettingsPanel', () => {
  it('reloads backend-owned server and grant state and only reports credential presence', async () => {
    const api = {
      listMCPServers: vi.fn(async () => ({ servers: [{
        server_id: 'srv-1',
        display_name: '育种数据',
        routing_description: '查询育种数据',
        endpoint_url: 'https://mcp.example.test',
        transport: 'streamable_http',
        protocol_preference: 'auto',
        auth_type: 'bearer',
        auth_metadata: {},
        enabled: true,
        health_status: 'available',
        credential_configured: true,
        config_version: 1,
        security_version: 1,
        last_tested_at: null,
        last_test_error_code: null,
        created_at: '2026-08-12T00:00:00Z',
        updated_at: '2026-08-12T00:00:00Z',
      }] })),
      createMCPServer: vi.fn(),
      patchMCPServer: vi.fn(),
      testMCPServer: vi.fn(async () => undefined),
      deleteMCPServer: vi.fn(async () => undefined),
      listMCPGrants: vi.fn(async () => ({ grants: [{
        grant_id: 'grant-1',
        server_id: 'srv-1',
        server_display_name: '育种数据',
        tool_name: '查询品系',
        granted_at: '2026-08-12T00:00:00Z',
        valid: true,
        invalid_reason: null,
      }] })),
      deleteMCPGrant: vi.fn(async () => undefined),
      clearMCPServerGrants: vi.fn(async () => undefined),
    } as unknown as Pick<ApiClient,
      | 'listMCPServers'
      | 'createMCPServer'
      | 'patchMCPServer'
      | 'testMCPServer'
      | 'deleteMCPServer'
      | 'listMCPGrants'
      | 'deleteMCPGrant'
      | 'clearMCPServerGrants'
    >;

    render(<MCPSettingsPanel api={api} />);

    expect(await screen.findByText('育种数据')).toBeInTheDocument();
    expect(screen.getByText(/凭据已配置/)).toBeInTheDocument();
    expect(screen.getByText('查询品系')).toBeInTheDocument();
    expect(screen.queryByText(/token|secret/i)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '撤销' }));
    await waitFor(() => expect(api.deleteMCPGrant).toHaveBeenCalledWith('grant-1'));
  });

  it('requires one confirmation before creating a public HTTP server', async () => {
    const createMCPServer = vi.fn(async () => ({} as never));
    const api = emptySettingsApi({ createMCPServer });
    render(<MCPSettingsPanel api={api} />);
    await openCreateForm();

    fireEvent.change(screen.getByLabelText('显示名称'), { target: { value: 'OCR' } });
    fireEvent.change(screen.getByLabelText('Endpoint URL'), {
      target: { value: 'http://175.6.25.109:51789/mcp' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'OK' }));

    expect(await screen.findByText('确认使用明文 HTTP')).toBeInTheDocument();
    expect(createMCPServer).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: '接受风险并保存' }));
    await waitFor(() => expect(createMCPServer).toHaveBeenCalledTimes(1));
    expect(createMCPServer).toHaveBeenCalledWith(expect.objectContaining({
      display_name: 'OCR',
      endpoint_url: 'http://175.6.25.109:51789/mcp',
    }));
  });

  it('submits HTTPS directly without showing the HTTP warning', async () => {
    const createMCPServer = vi.fn(async () => ({} as never));
    const api = emptySettingsApi({ createMCPServer });
    render(<MCPSettingsPanel api={api} />);
    await openCreateForm();

    fireEvent.change(screen.getByLabelText('显示名称'), { target: { value: 'Secure MCP' } });
    fireEvent.change(screen.getByLabelText('Endpoint URL'), {
      target: { value: 'https://mcp.example.test/mcp' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'OK' }));

    await waitFor(() => expect(createMCPServer).toHaveBeenCalledTimes(1));
    expect(screen.queryByText('确认使用明文 HTTP')).not.toBeInTheDocument();
  });

  it('keeps the form open and maps endpoint policy errors inside the modal', async () => {
    const createMCPServer = vi.fn(async () => {
      throw new ApiError(
        422,
        { detail: { code: 'mcp_endpoint_private_not_allowlisted' } },
        '请求未完成，请稍后重试。',
      );
    });
    const api = emptySettingsApi({ createMCPServer });
    render(<MCPSettingsPanel api={api} />);
    await openCreateForm();

    fireEvent.change(screen.getByLabelText('显示名称'), { target: { value: 'Private MCP' } });
    fireEvent.change(screen.getByLabelText('Endpoint URL'), {
      target: { value: 'https://10.2.3.4/mcp' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'OK' }));

    expect(await screen.findByText('该地址解析到不允许访问的私网地址。')).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: '添加 MCP 服务' })).toBeInTheDocument();
    expect(screen.getByLabelText('显示名称')).toHaveValue('Private MCP');
    expect(screen.getByRole('button', { name: 'OK' })).toBeEnabled();
  });
});
