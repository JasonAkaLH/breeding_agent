import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Form, Input, List, Modal, Select, Space, Switch, Tag, Typography } from 'antd';
import type { ApiClient } from '../api/client';
import type { CreateMCPServerRequest, MCPServerResponse, MCPToolGrantResponse, PatchMCPServerRequest } from '../api/types';

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

interface Props {
  api: SettingsApi;
  onError?(error: unknown): void;
}

interface ServerFormValue {
  display_name: string;
  routing_description: string;
  endpoint_url: string;
  transport: 'streamable_http' | 'legacy_http_sse';
  protocol_preference: string;
  auth_type: 'none' | 'bearer' | 'api_key_header' | 'static_headers';
  api_key_header_name?: string;
  credential_secret?: string;
  static_headers?: Array<{ name?: string; value?: string }>;
  enabled: boolean;
}

export function MCPSettingsPanel({ api, onError }: Props) {
  const [form] = Form.useForm<ServerFormValue>();
  const [servers, setServers] = useState<MCPServerResponse[]>([]);
  const [grants, setGrants] = useState<MCPToolGrantResponse[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testingId, setTestingId] = useState<string | null>(null);
  const [editing, setEditing] = useState<MCPServerResponse | null>(null);
  const [formOpen, setFormOpen] = useState(false);
  const [httpRiskOpen, setHttpRiskOpen] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState('');

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const [serverResult, grantResult] = await Promise.all([api.listMCPServers(), api.listMCPGrants()]);
      setServers(serverResult.servers);
      setGrants(grantResult.grants);
    } catch (error) {
      onError?.(error);
    } finally {
      setLoading(false);
    }
  }, [api, onError]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  function openCreate() {
    setEditing(null);
    setSaveError(null);
    setHttpRiskOpen(false);
    form.setFieldsValue({
      display_name: '',
      routing_description: '',
      endpoint_url: '',
      transport: 'streamable_http',
      protocol_preference: 'auto',
      auth_type: 'none',
      credential_secret: '',
      api_key_header_name: 'X-API-Key',
      static_headers: [{ name: '', value: '' }],
      enabled: true,
    });
    setFormOpen(true);
  }

  function openEdit(server: MCPServerResponse) {
    setEditing(server);
    setSaveError(null);
    setHttpRiskOpen(false);
    form.setFieldsValue({
      display_name: server.display_name,
      routing_description: server.routing_description,
      endpoint_url: server.endpoint_url,
      transport: server.transport === 'legacy_http_sse' ? 'legacy_http_sse' : 'streamable_http',
      protocol_preference: server.protocol_preference,
      auth_type: server.auth_type === 'bearer' || server.auth_type === 'api_key_header' || server.auth_type === 'static_headers'
        ? server.auth_type
        : 'none',
      api_key_header_name: typeof server.auth_metadata.header_name === 'string' ? server.auth_metadata.header_name : 'X-API-Key',
      credential_secret: '',
      static_headers: Array.isArray(server.auth_metadata.header_names)
        ? server.auth_metadata.header_names.map((name) => ({ name: String(name), value: '' }))
        : [{ name: '', value: '' }],
      enabled: server.enabled,
    });
    setFormOpen(true);
  }

  async function save(values: ServerFormValue, httpRiskConfirmed = false) {
    if (saving || (httpRiskOpen && !httpRiskConfirmed)) return;
    setSaveError(null);
    const staticHeaders = Object.fromEntries(
      (values.static_headers ?? [])
        .filter((entry) => entry.name?.trim() && entry.value)
        .map((entry) => [entry.name!.trim(), entry.value!]),
    );
    const replacementCredential = values.auth_type === 'static_headers'
      ? (Object.keys(staticHeaders).length > 0 ? { static_headers: staticHeaders } : undefined)
      : (values.credential_secret ? { secret_value: values.credential_secret } : undefined);
    const authMetadata = values.auth_type === 'api_key_header'
      ? { header_name: values.api_key_header_name?.trim() || 'X-API-Key' }
      : undefined;
    if (!editing && values.auth_type !== 'none' && !replacementCredential) {
      form.setFields([{ name: values.auth_type === 'static_headers' ? ['static_headers', 0, 'value'] : 'credential_secret', errors: ['请填写认证凭据'] }]);
      return;
    }
    const authTypeChanged = Boolean(editing && editing.auth_type !== values.auth_type);
    if (editing && authTypeChanged && values.auth_type !== 'none' && !replacementCredential) {
      form.setFields([{ name: values.auth_type === 'static_headers' ? ['static_headers', 0, 'value'] : 'credential_secret', errors: ['更换认证方式时必须填写新凭据'] }]);
      return;
    }
    if (!httpRiskConfirmed && requiresHttpRiskConfirmation(values.endpoint_url, editing?.endpoint_url)) {
      setHttpRiskOpen(true);
      return;
    }
    setSaving(true);
    try {
      if (editing) {
        const credentialAction = values.auth_type === 'none' && authTypeChanged
          ? 'clear'
          : replacementCredential ? 'replace' : 'retain';
        const patch: PatchMCPServerRequest = {
          display_name: values.display_name,
          routing_description: values.routing_description,
          endpoint_url: values.endpoint_url.trim(),
          transport: values.transport,
          protocol_preference: values.protocol_preference,
          auth_type: values.auth_type,
          ...(authMetadata ? { auth_metadata: authMetadata } : {}),
          enabled: values.enabled,
          credential_action: credentialAction,
          ...(replacementCredential ? { credential: replacementCredential } : {}),
        };
        await api.patchMCPServer(editing.server_id, patch);
      } else {
        const input: CreateMCPServerRequest = {
          display_name: values.display_name,
          routing_description: values.routing_description,
          endpoint_url: values.endpoint_url.trim(),
          transport: values.transport,
          protocol_preference: values.protocol_preference,
          auth_type: values.auth_type,
          ...(authMetadata ? { auth_metadata: authMetadata } : {}),
          enabled: values.enabled,
          ...(values.auth_type !== 'none' && replacementCredential
            ? { credential: replacementCredential }
            : {}),
        };
        await api.createMCPServer(input);
      }
      setFormOpen(false);
      form.resetFields();
      setAnnouncement(editing ? 'MCP 服务配置已更新' : 'MCP 服务已保存，正在等待健康检查');
      await refresh();
    } catch (error) {
      setSaveError(mcpConfigErrorMessage(error));
    } finally {
      setSaving(false);
    }
  }

  function cancelForm() {
    setHttpRiskOpen(false);
    setSaveError(null);
    setFormOpen(false);
  }

  async function confirmHttpRisk() {
    if (saving) return;
    setHttpRiskOpen(false);
    try {
      const values = await form.validateFields();
      await save(values, true);
    } catch {
      return;
    }
  }

  async function testServer(serverId: string) {
    setTestingId(serverId);
    try {
      await api.testMCPServer(serverId);
      setAnnouncement('MCP 服务重测已完成');
      await refresh();
    } catch (error) {
      onError?.(error);
    } finally {
      setTestingId(null);
    }
  }

  async function deleteServer(serverId: string) {
    try {
      await api.deleteMCPServer(serverId);
      setAnnouncement('MCP 服务已删除或进入待删除状态');
      await refresh();
    } catch (error) {
      onError?.(error);
    }
  }

  async function deleteGrant(grantId: string) {
    try {
      await api.deleteMCPGrant(grantId);
      setAnnouncement('工具授权已撤销');
      await refresh();
    } catch (error) {
      onError?.(error);
    }
  }

  async function clearGrants(serverId: string) {
    try {
      await api.clearMCPServerGrants(serverId);
      setAnnouncement('该 MCP 服务的工具授权已清空');
      await refresh();
    } catch (error) {
      onError?.(error);
    }
  }

  return (
    <section aria-labelledby="mcp-settings-title">
      <Space direction="vertical" size="middle" style={{ width: '100%' }}>
        <Space wrap>
          <Typography.Title id="mcp-settings-title" level={3} style={{ margin: 0 }}>MCP 服务</Typography.Title>
          <Button type="primary" onClick={openCreate}>添加 MCP 服务</Button>
          <Button onClick={() => void refresh()} loading={loading}>刷新</Button>
        </Space>
        <div role="status" aria-live="polite" aria-atomic="true">{announcement}</div>
        <List
          loading={loading}
          dataSource={servers}
          locale={{ emptyText: '尚未配置 MCP 服务' }}
          renderItem={(server) => (
            <List.Item
              actions={[
                <Button key="edit" aria-label={`编辑 ${server.display_name}`} size="small" onClick={() => openEdit(server)}>编辑</Button>,
                <Button key="test" aria-label={`重测 ${server.display_name}`} size="small" loading={testingId === server.server_id} onClick={() => void testServer(server.server_id)}>重测</Button>,
                <Button key="clear" aria-label={`清空 ${server.display_name} 的授权`} size="small" onClick={() => void clearGrants(server.server_id)}>清空授权</Button>,
                <Button key="delete" aria-label={`删除 ${server.display_name}`} size="small" danger onClick={() => void deleteServer(server.server_id)}>删除</Button>,
              ]}
            >
              <List.Item.Meta
                title={<Space>{server.display_name}<HealthTag status={server.health_status} />{server.enabled ? null : <Tag>已停用</Tag>}</Space>}
                description={`${server.transport} · 凭据${server.credential_configured ? '已配置' : '未配置'}`}
              />
            </List.Item>
          )}
        />
        <Card size="small" title="始终允许的工具">
          <List
            dataSource={grants}
            locale={{ emptyText: '暂无长期工具授权' }}
            renderItem={(grant) => (
              <List.Item actions={[<Button key="revoke" aria-label="撤销" size="small" onClick={() => void deleteGrant(grant.grant_id)}>撤销</Button>]}>
                <List.Item.Meta
                  title={<Space>{grant.tool_name}<Tag color={grant.valid ? 'green' : 'red'}>{grant.valid ? '有效' : '已失效'}</Tag></Space>}
                  description={`${grant.server_display_name} · ${grant.invalid_reason || (grant.granted_at ? new Date(grant.granted_at).toLocaleString() : '授权时间未知')}`}
                />
              </List.Item>
            )}
          />
        </Card>
      </Space>
      <Modal
        open={formOpen}
        title={editing ? '编辑 MCP 服务' : '添加 MCP 服务'}
        onCancel={cancelForm}
        onOk={() => form.submit()}
        confirmLoading={saving}
        okButtonProps={{ disabled: httpRiskOpen }}
        destroyOnHidden
        focusTriggerAfterClose
      >
        <Alert type="info" showIcon message="凭据保存后不会再次显示；留空表示保留现有凭据。" />
        {saveError ? (
          <Alert
            type="error"
            showIcon
            closable
            onClose={() => setSaveError(null)}
            message={saveError}
            style={{ marginTop: 12 }}
          />
        ) : null}
        <Form form={form} layout="vertical" onFinish={(values) => void save(values)}>
          <Form.Item name="display_name" label="显示名称" rules={[{ required: true, message: '请输入显示名称' }]}>
            <Input autoFocus maxLength={100} />
          </Form.Item>
          <Form.Item name="routing_description" label="路由描述">
            <Input.TextArea maxLength={2000} rows={3} />
          </Form.Item>
          <Form.Item name="endpoint_url" label="Endpoint URL" rules={[{ required: true, message: '请输入 Endpoint URL' }]}>
            <Input type="url" autoComplete="off" />
          </Form.Item>
          <Form.Item name="transport" label="传输方式" rules={[{ required: true }]}>
            <Select options={[
              { value: 'streamable_http', label: 'Streamable HTTP' },
              { value: 'legacy_http_sse', label: 'Legacy HTTP SSE' },
            ]} />
          </Form.Item>
          <Form.Item name="protocol_preference" label="协议偏好" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="auth_type" label="认证方式" rules={[{ required: true }]}>
            <Select options={[
              { value: 'none', label: '无认证' },
              { value: 'bearer', label: 'Bearer Token' },
              { value: 'api_key_header', label: 'API Key Header' },
              { value: 'static_headers', label: '多个静态 Header' },
            ]} />
          </Form.Item>
          <Form.Item noStyle shouldUpdate={(previous, current) => previous.auth_type !== current.auth_type}>
            {({ getFieldValue }) => {
              const authType = getFieldValue('auth_type');
              if (authType === 'none') return null;
              if (authType === 'static_headers') {
                return (
                  <Form.List name="static_headers">
                    {(fields, { add, remove }) => (
                      <Space direction="vertical" style={{ width: '100%' }}>
                        <Typography.Text strong>静态 Header</Typography.Text>
                        {fields.map((field) => (
                          <Space key={field.key} align="baseline" wrap>
                            <Form.Item {...field} name={[field.name, 'name']} rules={[{ required: true, message: '请输入 Header 名称' }]}>
                              <Input aria-label="Header 名称" placeholder="Header 名称" autoComplete="off" />
                            </Form.Item>
                            <Form.Item {...field} name={[field.name, 'value']}>
                              <Input.Password aria-label="Header 值" placeholder="Header 值" autoComplete="new-password" />
                            </Form.Item>
                            <Button aria-label="删除 Header" onClick={() => remove(field.name)}>删除</Button>
                          </Space>
                        ))}
                        <Button onClick={() => add({ name: '', value: '' })}>添加 Header</Button>
                      </Space>
                    )}
                  </Form.List>
                );
              }
              return (
                <>
                  {authType === 'api_key_header' ? (
                    <Form.Item name="api_key_header_name" label="API Key Header 名称" rules={[{ required: true, message: '请输入 Header 名称' }]}>
                      <Input autoComplete="off" />
                    </Form.Item>
                  ) : null}
                  <Form.Item name="credential_secret" label={authType === 'bearer' ? 'Bearer Token' : 'API Key'}>
                    <Input.Password autoComplete="new-password" />
                  </Form.Item>
                </>
              );
            }}
          </Form.Item>
          <Form.Item name="enabled" label="参与自动路由" valuePropName="checked">
            <Switch />
          </Form.Item>
        </Form>
      </Modal>
      <Modal
        open={httpRiskOpen}
        title="确认使用明文 HTTP"
        okText="接受风险并保存"
        cancelText="取消"
        onOk={() => void confirmHttpRisk()}
        onCancel={() => setHttpRiskOpen(false)}
        confirmLoading={saving}
        destroyOnHidden
        focusTriggerAfterClose
      >
        <Alert
          type="warning"
          showIcon
          message="此 MCP Server 未使用 TLS 加密"
          description="MCP 请求、响应以及 Bearer Token 或 API Key 可能被网络链路观察或篡改。继续表示你接受当前 Endpoint 的明文传输风险。"
        />
      </Modal>
    </section>
  );
}

function endpointProtocol(value: string | undefined): string | null {
  if (!value) return null;
  try {
    return new URL(value.trim()).protocol.toLowerCase();
  } catch {
    return null;
  }
}

function requiresHttpRiskConfirmation(endpoint: string, previousEndpoint?: string): boolean {
  if (endpointProtocol(endpoint) !== 'http:') return false;
  if (!previousEndpoint) return true;
  return endpointProtocol(previousEndpoint) === 'https:';
}

function mcpConfigErrorMessage(error: unknown): string {
  const responseDetail = error && typeof error === 'object' && 'detail' in error
    ? (error as { detail?: unknown }).detail
    : null;
  const detail = responseDetail && typeof responseDetail === 'object' && 'detail' in responseDetail
    ? (responseDetail as { detail?: unknown }).detail
    : null;
  const code = detail && typeof detail === 'object' && 'code' in detail
    ? String((detail as { code?: unknown }).code || '')
    : '';
  const messages: Record<string, string> = {
    mcp_endpoint_private_forbidden: '该地址解析到不允许访问的私网地址。',
    mcp_endpoint_private_not_allowlisted: '该地址解析到不允许访问的私网地址。',
    mcp_endpoint_ip_forbidden: '该地址属于回环、链路本地、云元数据或其他禁止网段。',
    mcp_endpoint_dns_failed: '无法解析 MCP Server 地址。',
    mcp_endpoint_dns_rebinding: 'MCP Server 的 DNS 解析结果发生了不安全变化。',
    mcp_endpoint_redirect_cross_origin: 'MCP Server 返回了不允许的跨域重定向。',
    mcp_endpoint_redirect_downgrade: 'MCP Server 尝试从 HTTPS 降级到 HTTP。',
  };
  return messages[code] || 'MCP Server 配置未保存，请稍后重试。';
}

function HealthTag({ status }: { status: string }) {
  const color = status === 'available' || status === 'healthy' ? 'green' : status === 'testing' ? 'blue' : 'red';
  const label = status === 'available' || status === 'healthy' ? '可用' : status === 'testing' ? '测试中' : '不可用';
  return <Tag color={color}>{label}</Tag>;
}
