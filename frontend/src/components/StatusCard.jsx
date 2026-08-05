import React from 'react';

const statusColors = {
  green: 'bg-emerald-500',
  yellow: 'bg-yellow-500',
  red: 'bg-red-500',
  gray: 'bg-gray-500',
};

const statusGlow = {
  green: 'shadow-emerald-500/50',
  yellow: 'shadow-yellow-500/50',
  red: 'shadow-red-500/50',
  gray: 'shadow-gray-500/50',
};

export default function StatusCard({ title, status = 'gray', value, subtitle, icon: Icon }) {
  const dotColor = statusColors[status] || statusColors.gray;
  const glow = statusGlow[status] || statusGlow.gray;

  return (
    <div className="bg-gray-900 border border-gray-800 rounded-xl p-5 hover:border-gray-700 transition-colors">
      <div className="flex items-start justify-between">
        <div className="flex-1">
          <div className="flex items-center gap-2 mb-3">
            <div className={`w-2.5 h-2.5 rounded-full ${dotColor} shadow-lg ${glow}`} />
            <span className="text-sm font-medium text-gray-400 uppercase tracking-wide">
              {title}
            </span>
          </div>
          <div className="text-2xl font-semibold text-gray-100 mb-1">{value}</div>
          {subtitle && (
            <div className="text-sm text-gray-500">{subtitle}</div>
          )}
        </div>
        {Icon && (
          <div className="text-gray-600 ml-4">
            <Icon size={24} />
          </div>
        )}
      </div>
    </div>
  );
}
