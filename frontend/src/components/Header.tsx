import { ArrowUpRight, HeartHandshake } from 'lucide-react';
import { usingMockApi } from '../api';

interface HeaderProps {
  onGetStarted: () => void;
}

export function Header({ onGetStarted }: HeaderProps) {
  return (
    <header className="border-b border-line/60 bg-canvas/95 backdrop-blur">
      <nav className="px-5 py-5 sm:px-8" aria-label="Main navigation">
        <div className="mx-auto flex max-w-6xl items-center justify-between">
          <a className="group flex items-center gap-3 text-primary focus-visible:outline-none focus-visible:ring-4 focus-visible:ring-primary/20" href="#top">
            <span className="flex size-11 items-center justify-center rounded-2xl bg-primary text-white shadow-sm transition group-hover:-rotate-3 group-hover:scale-105">
              <HeartHandshake className="size-5" strokeWidth={2.4} aria-hidden="true" />
            </span>
            <span className="font-display text-[2rem] font-semibold leading-none tracking-tight sm:text-[2.25rem]">Tenderly</span>
          </a>

          <div className="flex items-center gap-3">
            {usingMockApi && (
              <span className="hidden rounded-full bg-primary-soft px-3 py-1 text-xs font-semibold text-primary-dark sm:inline">
                Demo mode
              </span>
            )}
            <button className="button-secondary hidden sm:inline-flex" onClick={onGetStarted} type="button">
              <HeartHandshake className="size-4" aria-hidden="true" />
              Find your fit
            </button>
            <button className="button-primary sm:hidden" onClick={onGetStarted} type="button" aria-label="Get started">
              <ArrowUpRight className="size-5" aria-hidden="true" />
            </button>
          </div>
        </div>
      </nav>
    </header>
  );
}
