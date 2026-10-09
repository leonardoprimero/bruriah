use super::popup::DatePickerPopup;
use core::ops::RangeInclusive;
use egui::{Area, Button, Frame, InnerResponse, Key, Order, RichText, Ui, Widget};
use jiff::civil::Date;

#[derive(Default, Clone)]
#[cfg_attr(feature = "serde", derive(serde::Deserialize, serde::Serialize))]
pub(crate) struct DatePickerButtonState {
    pub picker_visible: bool,
}

/// Convert a [`chrono::NaiveDate`] into the date the picker edits.
pub fn from_naive_date(date: chrono::NaiveDate) -> Option<Date> {
    use chrono::Datelike as _;
    Date::new(
        i16::try_from(date.year()).ok()?,
        i8::try_from(date.month()).ok()?,
        i8::try_from(date.day()).ok()?,
    )
    .ok()
}

/// Shows a date, and will open a date picker popup when clicked.
pub struct DatePickerButton<'a> {
    selection: &'a mut Date,
    id_salt: Option<&'a str>,
    combo_boxes: bool,
    arrows: bool,
    calendar: bool,
    calendar_week: bool,
    show_icon: bool,
    format: String,
    highlight_weekends: bool,
    start_end_years: Option<RangeInclusive<i16>>,
    reverse_years: bool,
    year_scroll_to: Option<i16>,
}

impl<'a> DatePickerButton<'a> {
    pub fn new(selection: &'a mut Date) -> Self {
        Self {
            selection,
            id_salt: None,
            combo_boxes: true,
            arrows: true,
            calendar: true,
            calendar_week: true,
            show_icon: true,
            format: "%Y-%m-%d".to_owned(),
            highlight_weekends: true,
            start_end_years: None,
            reverse_years: false,
            year_scroll_to: None,
        }
    }

    /// Add id source.
    /// Must be set if multiple date picker buttons are in the same Ui.
    #[inline]
    pub fn id_salt(mut self, id_salt: &'a str) -> Self {
        self.id_salt = Some(id_salt);
        self
    }

    /// Show combo boxes in date picker popup. (Default: true)
    #[inline]
    pub fn combo_boxes(mut self, combo_boxes: bool) -> Self {
        self.combo_boxes = combo_boxes;
        self
    }
}
