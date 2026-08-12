import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { MCPSettingsPanel } from './MCPSettingsPanel';
import type { ApiClient } from '../api/client';

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
});
