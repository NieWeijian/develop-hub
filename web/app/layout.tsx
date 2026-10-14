import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: '开发环境 · RK Test Hub',
  description: '为 RK 产品配置固件来源、测试方案与用例，执行测试并查看可追溯的报告',
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body><div role="status" style={{ background: "#fef3c7", color: "#78350f", padding: "10px 20px", textAlign: "center", fontWeight: 700 }}>开发环境 · 独立数据 · 仅模拟设备 · 真实设备操作已禁用</div>{children}</body>
    </html>
  );
}
