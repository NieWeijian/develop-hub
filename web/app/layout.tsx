import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Develop Hub · 175 Control Plane',
  description: '面向标准化软件研发闭环的 Agent 编排与过程控制平台',
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="zh-CN">
      <body>{children}</body>
    </html>
  );
}
