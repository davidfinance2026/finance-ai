import React, { useEffect, useMemo, useState, createContext, useContext } from 'react';
import {
  BrowserRouter,
  Navigate,
  NavLink,
  Outlet,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from 'react-router-dom';

const API_BASE_URL =
  process.env.REACT_APP_API_URL || 'http://127.0.0.1:5000';

const AuthContext = createContext(null);

function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error('useAuth must be used within AuthProvider');
  }
  return context;
}

async function apiRequest(path, options = {}) {
  const token = localStorage.getItem('finance_ai_token');

  const headers = {
    'Content-Type': 'application/json',
    ...(options.headers || {}),
  };

  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
  });

  const contentType = response.headers.get('content-type') || '';
  const isJson = contentType.includes('application/json');
  const data = isJson ? await response.json() : await response.text();

  if (!response.ok) {
    const message =
      (isJson && (data.message || data.error)) ||
      'Erro ao comunicar com o servidor.';
    throw new Error(message);
  }

  return data;
}

function AuthProvider({ children }) {
  const [token, setToken] = useState(
    localStorage.getItem('finance_ai_token') || ''
  );
  const [user, setUser] = useState(() => {
    const stored = localStorage.getItem('finance_ai_user');
    return stored ? JSON.parse(stored) : null;
  });
  const [loading, setLoading] = useState(Boolean(token));

  const login = async (email, password) => {
    const data = await apiRequest('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
      headers: { Authorization: '' },
    });

    localStorage.setItem('finance_ai_token', data.access_token);
    localStorage.setItem('finance_ai_user', JSON.stringify(data.user));
    setToken(data.access_token);
    setUser(data.user);
    return data;
  };

  const logout = () => {
    localStorage.removeItem('finance_ai_token');
    localStorage.removeItem('finance_ai_user');
    setToken('');
    setUser(null);
  };

  const loadProfile = async () => {
    if (!localStorage.getItem('finance_ai_token')) {
      setLoading(false);
      return;
    }

    try {
      const data = await apiRequest('/auth/me');
      setUser(data.user || data);
      localStorage.setItem(
        'finance_ai_user',
        JSON.stringify(data.user || data)
      );
    } catch (error) {
      logout();
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadProfile();
  }, []);

  const value = useMemo(
    () => ({
      token,
      user,
      loading,
      authenticated: Boolean(token && user),
      login,
      logout,
      reloadProfile: loadProfile,
    }),
    [token, user, loading]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

function ProtectedRoute() {
  const { authenticated, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return <FullPageMessage text="Carregando sessão..." />;
  }

  if (!authenticated) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  return <Outlet />;
}

function PublicOnlyRoute() {
  const { authenticated, loading } = useAuth();

  if (loading) {
    return <FullPageMessage text="Carregando..." />;
  }

  if (authenticated) {
    return <Navigate to="/dashboard" replace />;
  }

  return <Outlet />;
}

function Layout() {
  const { user, logout } = useAuth();
  const location = useLocation();

  const menu = [
    { to: '/dashboard', label: 'Dashboard' },
    { to: '/lancamentos', label: 'Lançamentos' },
    { to: '/investimentos', label: 'Investimentos' },
    { to: '/orcamentos', label: 'Orçamentos' },
    { to: '/assistente-financeiro', label: 'Assistente Financeiro' },
  ];

  return (
    <div style={styles.appShell}>
      <aside style={styles.sidebar}>
        <div>
          <h2 style={styles.brand}>Finance AI</h2>
          <p style={styles.sidebarSubtitle}>Gestão financeira inteligente</p>
        </div>

        <nav style={styles.nav}>
          {menu.map((item) => {
            const active = location.pathname === item.to;
            return (
              <NavLink
                key={item.to}
                to={item.to}
                style={{
                  ...styles.navLink,
                  ...(active ? styles.navLinkActive : {}),
                }}
              >
                {item.label}
              </NavLink>
            );
          })}
        </nav>

        <div style={styles.userBox}>
          <div>
            <div style={styles.userName}>{user?.name || 'Usuário'}</div>
            <div style={styles.userEmail}>{user?.email || ''}</div>
          </div>
          <button onClick={logout} style={styles.secondaryButton}>
            Sair
          </button>
        </div>
      </aside>

      <main style={styles.mainContent}>
        <Outlet />
      </main>
    </div>
  );
}

function LoginPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const { login } = useAuth();
  const [form, setForm] = useState({ email: '', password: '' });
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');

  const from = location.state?.from?.pathname || '/dashboard';

  const handleSubmit = async (event) => {
    event.preventDefault();
    setSubmitting(true);
    setError('');

    try {
      await login(form.email, form.password);
      navigate(from, { replace: true });
    } catch (err) {
      setError(err.message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div style={styles.loginWrapper}>
      <div style={styles.loginCard}>
        <h1 style={styles.pageTitle}>Finance AI</h1>
        <p style={styles.pageSubtitle}>Entre para acessar sua central financeira</p>

        <form onSubmit={handleSubmit} style={styles.form}>
          <input
            type="email"
            placeholder="Seu e-mail"
            value={form.email}
            onChange={(e) => setForm({ ...form, email: e.target.value })}
            style={styles.input}
            required
          />
          <input
            type="password"
            placeholder="Sua senha"
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
            style={styles.input}
            required
          />

          {error ? <div style={styles.errorBox}>{error}</div> : null}

          <button type="submit" disabled={submitting} style={styles.primaryButton}>
            {submitting ? 'Entrando...' : 'Entrar'}
          </button>
        </form>
      </div>
    </div>
  );
}

function DashboardPage() {
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    const loadData = async () => {
      try {
        const data = await apiRequest('/dashboard/summary');
        setSummary(data);
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    };

    loadData();
  }, []);

  if (loading) return <SectionLoading title="Dashboard" />;
  if (error) return <SectionError title="Dashboard" error={error} />;

  const cards = [
    { label: 'Saldo Atual', value: currency(summary?.saldo_atual) },
    { label: 'Receitas do Mês', value: currency(summary?.receitas_mes) },
    { label: 'Despesas do Mês', value: currency(summary?.despesas_mes) },
    { label: 'Patrimônio Investido', value: currency(summary?.patrimonio_investido) },
  ];

  return (
    <section>
      <PageHeader
        title="Dashboard"
        subtitle="Visão geral da sua vida financeira"
      />

      <div style={styles.grid4}>
        {cards.map((card) => (
          <div key={card.label} style={styles.card}>
            <div style={styles.cardLabel}>{card.label}</div>
            <div style={styles.cardValue}>{card.value}</div>
          </div>
        ))}
      </div>

      <div style={styles.card}>
        <h3>Resumo</h3>
        <p>
          {summary?.mensagem_geral ||
            'Seu backend pode retornar aqui um resumo financeiro consolidado.'}
        </p>
      </div>
    </section>
  );
}

function LancamentosPage() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [form, setForm] = useState({
    descricao: '',
    valor: '',
    tipo: 'despesa',
    categoria: '',
    data: new Date().toISOString().slice(0, 10),
  });
  const [saving, setSaving] = useState(false);

  const loadItems = async () => {
    try {
      setLoading(true);
      const data = await apiRequest('/lancamentos');
      setItems(Array.isArray(data) ? data : data.items || []);
      setError('');
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    loadItems();
  }, []);

  const handleSubmit = async (event) => {
    event.preventDefault();
    setSaving(true);

    try {
      await apiRequest('/lancamentos', {
        method: 'POST',
        body: JSON.stringify({
          ...form,
          valor: Number(form.valor),
        }),
      });

      setForm({
        descricao: '',
        valor: '',
        tipo: 'despesa',
        categoria: '',
        data: new Date().toISOString().slice(0, 10),
      });
      await loadItems();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <section>
      <PageHeader
        title="Lançamentos"
        subtitle="Cadastre receitas e despesas do dia a dia"
      />

      <div style={styles.twoColumn}>
        <div style={styles.card}>
          <h3>Novo lançamento</h3>
          <form onSubmit={handleSubmit} style={styles.form}>
            <input
              style={styles.input}
              placeholder="Descrição"
              value={form.descricao}
              onChange={(e) => setForm({ ...form, descricao: e.target.value })}
              required
            />
            <input
              style={styles.input}
              placeholder="Valor"
              type="number"
              step="0.01"
              value={form.valor}
              onChange={(e) => setForm({ ...form, valor: e.target.value })}
              required
            />
            <select
              style={styles.input}
              value={form.tipo}
              onChange={(e) => setForm({ ...form, tipo: e.target.value })}
            >
              <option value="despesa">Despesa</option>
              <option value="receita">Receita</option>
            </select>
            <input
              style={styles.input}
              placeholder="Categoria"
              value={form.categoria}
              onChange={(e) => setForm({ ...form, categoria: e.target.value })}
            />
            <input
              style={styles.input}
              type="date"
              value={form.data}
              onChange={(e) => setForm({ ...form, data: e.target.value })}
            />
            <button disabled={saving} style={styles.primaryButton}>
              {saving ? 'Salvando...' : 'Salvar lançamento'}
            </button>
          </form>
        </div>

        <div style={styles.card}>
          <h3>Histórico</h3>
          {loading ? (
            <p>Carregando lançamentos...</p>
          ) : error ? (
            <div style={styles.errorBox}>{error}</div>
          ) : items.length === 0 ? (
            <p>Nenhum lançamento cadastrado.</p>
          ) : (
            <div style={styles.list}>
              {items.map((item) => (
                <div key={item.id} style={styles.listItem}>
                  <div>
                    <strong>{item.descricao}</strong>
                    <div style={styles.mutedText}>
                      {item.categoria || 'Sem categoria'} • {item.data}
                    </div>
                  </div>
                  <div
                    style={{
                      ...styles.valuePill,
                      color: item.tipo === 'receita' ? '#0f9d58' : '#c62828',
                    }}
                  >
                    {item.tipo === 'receita' ? '+' : '-'} {currency(item.valor)}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function InvestimentosPage() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    const loadData = async () => {
      try {
        const data = await apiRequest('/investimentos');
        setItems(Array.isArray(data) ? data : data.items || []);
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    };

    loadData();
  }, []);

  return (
    <section>
      <PageHeader
        title="Investimentos"
        subtitle="Acompanhe carteira, rentabilidade e alocação"
      />

      <div style={styles.card}>
        {loading ? (
          <p>Carregando investimentos...</p>
        ) : error ? (
          <div style={styles.errorBox}>{error}</div>
        ) : items.length === 0 ? (
          <p>Nenhum investimento encontrado.</p>
        ) : (
          <div style={styles.list}>
            {items.map((item) => (
              <div key={item.id} style={styles.listItem}>
                <div>
                  <strong>{item.nome}</strong>
                  <div style={styles.mutedText}>
                    {item.tipo || 'Ativo'} • {item.corretora || 'Sem instituição'}
                  </div>
                </div>
                <div style={{ textAlign: 'right' }}>
                  <div style={styles.cardValueSmall}>{currency(item.valor_atual)}</div>
                  <div style={styles.mutedText}>
                    Rentab.: {item.rentabilidade ?? 0}%
                  </div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function OrcamentosPage() {
  const [items, setItems] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  useEffect(() => {
    const loadData = async () => {
      try {
        const data = await apiRequest('/orcamentos');
        setItems(Array.isArray(data) ? data : data.items || []);
      } catch (err) {
        setError(err.message);
      } finally {
        setLoading(false);
      }
    };

    loadData();
  }, []);

  return (
    <section>
      <PageHeader
        title="Orçamentos"
        subtitle="Controle metas por categoria e acompanhe limites"
      />

      <div style={styles.grid2}>
        {loading ? (
          <div style={styles.card}>Carregando orçamentos...</div>
        ) : error ? (
          <div style={styles.card}>
            <div style={styles.errorBox}>{error}</div>
          </div>
        ) : items.length === 0 ? (
          <div style={styles.card}>Nenhum orçamento cadastrado.</div>
        ) : (
          items.map((item) => {
            const percentual = Math.min(
              100,
              Math.round(((item.gasto_atual || 0) / (item.limite || 1)) * 100)
            );

            return (
              <div key={item.id} style={styles.card}>
                <div style={styles.listItem}>
                  <strong>{item.categoria}</strong>
                  <span>{percentual}%</span>
                </div>
                <div style={styles.progressTrack}>
                  <div
                    style={{ ...styles.progressFill, width: `${percentual}%` }}
                  />
                </div>
                <div style={styles.mutedText}>
                  Gasto: {currency(item.gasto_atual)} de {currency(item.limite)}
                </div>
              </div>
            );
          })
        )}
      </div>
    </section>
  );
}

function AssistenteFinanceiroPage() {
  const [question, setQuestion] = useState('');
  const [answer, setAnswer] = useState('');
  const [history, setHistory] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const handleAsk = async (event) => {
    event.preventDefault();
    if (!question.trim()) return;

    const currentQuestion = question.trim();
    setLoading(true);
    setError('');

    try {
      const data = await apiRequest('/assistente-financeiro', {
        method: 'POST',
        body: JSON.stringify({ pergunta: currentQuestion }),
      });

      const responseText =
        data.resposta || data.answer || 'Resposta recebida com sucesso.';

      setAnswer(responseText);
      setHistory((prev) => [
        { pergunta: currentQuestion, resposta: responseText, id: Date.now() },
        ...prev,
      ]);
      setQuestion('');
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  return (
    <section>
      <PageHeader
        title="Assistente Financeiro"
        subtitle="Faça perguntas sobre gastos, metas e decisões financeiras"
      />

      <div style={styles.twoColumn}>
        <div style={styles.card}>
          <form onSubmit={handleAsk} style={styles.form}>
            <textarea
              style={styles.textarea}
              rows={6}
              placeholder="Ex.: Como posso reduzir meus gastos fixos em 10% este mês?"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
            />
            {error ? <div style={styles.errorBox}>{error}</div> : null}
            <button style={styles.primaryButton} disabled={loading}>
              {loading ? 'Consultando...' : 'Perguntar ao assistente'}
            </button>
          </form>

          {answer ? (
            <div style={{ ...styles.card, marginTop: 16 }}>
              <h3>Resposta</h3>
              <p style={{ whiteSpace: 'pre-wrap' }}>{answer}</p>
            </div>
          ) : null}
        </div>

        <div style={styles.card}>
          <h3>Histórico recente</h3>
          {history.length === 0 ? (
            <p>Nenhuma pergunta enviada ainda.</p>
          ) : (
            <div style={styles.list}>
              {history.map((item) => (
                <div key={item.id} style={styles.listItemColumn}>
                  <strong>Pergunta:</strong>
                  <span>{item.pergunta}</span>
                  <strong style={{ marginTop: 8 }}>Resposta:</strong>
                  <span>{item.resposta}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function NotFoundPage() {
  return (
    <FullPageMessage text="Página não encontrada. Redirecione para uma rota válida." />
  );
}

function PageHeader({ title, subtitle }) {
  return (
    <div style={styles.pageHeader}>
      <h1 style={styles.pageTitle}>{title}</h1>
      <p style={styles.pageSubtitle}>{subtitle}</p>
    </div>
  );
}

function SectionLoading({ title }) {
  return (
    <section>
      <PageHeader title={title} subtitle="Carregando informações..." />
      <div style={styles.card}>Aguarde um instante.</div>
    </section>
  );
}

function SectionError({ title, error }) {
  return (
    <section>
      <PageHeader title={title} subtitle="Ocorreu um problema ao carregar os dados" />
      <div style={styles.errorBox}>{error}</div>
    </section>
  );
}

function FullPageMessage({ text }) {
  return (
    <div style={styles.fullPageCenter}>
      <div style={styles.card}>{text}</div>
    </div>
  );
}

function currency(value) {
  return new Intl.NumberFormat('pt-BR', {
    style: 'currency',
    currency: 'BRL',
  }).format(Number(value || 0));
}

export default function App() {
  return (
    <AuthProvider>
      <BrowserRouter>
        <Routes>
          <Route element={<PublicOnlyRoute />}>
            <Route path="/login" element={<LoginPage />} />
          </Route>

          <Route element={<ProtectedRoute />}>
            <Route element={<Layout />}>
              <Route path="/" element={<Navigate to="/dashboard" replace />} />
              <Route path="/dashboard" element={<DashboardPage />} />
              <Route path="/lancamentos" element={<LancamentosPage />} />
              <Route path="/investimentos" element={<InvestimentosPage />} />
              <Route path="/orcamentos" element={<OrcamentosPage />} />
              <Route
                path="/assistente-financeiro"
                element={<AssistenteFinanceiroPage />}
              />
            </Route>
          </Route>

          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </BrowserRouter>
    </AuthProvider>
  );
}

const styles = {
  appShell: {
    display: 'grid',
    gridTemplateColumns: '260px 1fr',
    minHeight: '100vh',
    background: '#f6f8fb',
    color: '#1f2937',
  },
  sidebar: {
    background: '#111827',
    color: '#ffffff',
    padding: '24px 18px',
    display: 'flex',
    flexDirection: 'column',
    justifyContent: 'space-between',
    gap: 24,
  },
  brand: {
    margin: 0,
    fontSize: 24,
  },
  sidebarSubtitle: {
    marginTop: 8,
    color: '#9ca3af',
    fontSize: 14,
  },
  nav: {
    display: 'flex',
    flexDirection: 'column',
    gap: 10,
  },
  navLink: {
    color: '#d1d5db',
    textDecoration: 'none',
    padding: '12px 14px',
    borderRadius: 10,
    fontWeight: 500,
  },
  navLinkActive: {
    background: '#2563eb',
    color: '#fff',
  },
  userBox: {
    borderTop: '1px solid #374151',
    paddingTop: 16,
    display: 'flex',
    flexDirection: 'column',
    gap: 12,
  },
  userName: {
    fontWeight: 700,
  },
  userEmail: {
    color: '#9ca3af',
    fontSize: 14,
  },
  mainContent: {
    padding: 24,
  },
  loginWrapper: {
    minHeight: '100vh',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    background: 'linear-gradient(135deg, #eff6ff, #f8fafc)',
    padding: 16,
  },
  loginCard: {
    width: '100%',
    maxWidth: 420,
    background: '#fff',
    borderRadius: 16,
    padding: 28,
    boxShadow: '0 14px 40px rgba(15, 23, 42, 0.08)',
  },
  form: {
    display: 'flex',
    flexDirection: 'column',
    gap: 12,
  },
  input: {
    padding: '12px 14px',
    borderRadius: 10,
    border: '1px solid #d1d5db',
    fontSize: 14,
    outline: 'none',
  },
  textarea: {
    padding: '12px 14px',
    borderRadius: 10,
    border: '1px solid #d1d5db',
    fontSize: 14,
    resize: 'vertical',
    fontFamily: 'inherit',
  },
  primaryButton: {
    background: '#2563eb',
    color: '#fff',
    border: 'none',
    borderRadius: 10,
    padding: '12px 16px',
    fontSize: 14,
    fontWeight: 700,
    cursor: 'pointer',
  },
  secondaryButton: {
    background: '#1f2937',
    color: '#fff',
    border: '1px solid #4b5563',
    borderRadius: 10,
    padding: '10px 14px',
    fontSize: 14,
    cursor: 'pointer',
  },
  pageHeader: {
    marginBottom: 20,
  },
  pageTitle: {
    margin: 0,
    fontSize: 28,
    fontWeight: 800,
  },
  pageSubtitle: {
    color: '#6b7280',
    marginTop: 6,
  },
  card: {
    background: '#fff',
    borderRadius: 16,
    padding: 18,
    boxShadow: '0 8px 24px rgba(15, 23, 42, 0.06)',
    marginBottom: 16,
  },
  cardLabel: {
    color: '#6b7280',
    fontSize: 14,
    marginBottom: 8,
  },
  cardValue: {
    fontSize: 28,
    fontWeight: 800,
  },
  cardValueSmall: {
    fontSize: 18,
    fontWeight: 800,
  },
  grid4: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
    gap: 16,
    marginBottom: 16,
  },
  grid2: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))',
    gap: 16,
  },
  twoColumn: {
    display: 'grid',
    gridTemplateColumns: 'minmax(320px, 420px) 1fr',
    gap: 16,
    alignItems: 'start',
  },
  list: {
    display: 'flex',
    flexDirection: 'column',
    gap: 12,
  },
  listItem: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    gap: 12,
    padding: '12px 0',
    borderBottom: '1px solid #e5e7eb',
  },
  listItemColumn: {
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
    padding: '12px 0',
    borderBottom: '1px solid #e5e7eb',
  },
  mutedText: {
    color: '#6b7280',
    fontSize: 14,
  },
  valuePill: {
    background: '#f9fafb',
    borderRadius: 999,
    padding: '8px 12px',
    fontWeight: 700,
    whiteSpace: 'nowrap',
  },
  progressTrack: {
    width: '100%',
    height: 10,
    background: '#e5e7eb',
    borderRadius: 999,
    overflow: 'hidden',
    margin: '10px 0',
  },
  progressFill: {
    height: '100%',
    background: '#2563eb',
    borderRadius: 999,
  },
  errorBox: {
    background: '#fef2f2',
    color: '#b91c1c',
    border: '1px solid #fecaca',
    borderRadius: 10,
    padding: '12px 14px',
  },
  fullPageCenter: {
    minHeight: '100vh',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 24,
    background: '#f6f8fb',
  },
};
