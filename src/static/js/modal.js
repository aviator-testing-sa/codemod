/**
 * Modal functions
 */

(function ($) {
    $.fn.openmodal = function(args) {
        var self = this;
        self.$modal = $('#primary-modal');
        self.options = {
            title: args.title || '',
            url: args.url || '',
            html: args.html || '',
            showBtns: args.showBtns,
            success: args.success,
            hideSecondary: args.hideSecondary,
            onLoad: args.onLoad,
        };
        // reset
        self.$modal.find('.btn-default').removeClass('hidden');
        self.$modal.find('.modal-footer').removeClass('hidden');
        self.$modal.find('.btn-primary').removeClass('btn-loading');
        self.$modal.find('.btn-primary').off('click');
        if (self.$modal.hasClass('in')) {
            self.$modal.modal("hide");
        }

        self.$modal.find('.modal-title').html(self.options.title);
        self.$modal.find('.modal-footer').addClass('hidden');
        if (self.options.hideSecondary) {
            self.$modal.find('.btn-default').addClass('hidden');
        }
        if (self.options.html) {
            self.$modal.find('.modal-body').html(self.options.html);
        } else if (self.options.url) {
            self.$modal.find('.modal-body').html(
                '<div class="loading">' +
                    '<i class="fa fa-refresh fa-spin fa-3x fa-fw"></i>' +
                '</div>');
            // Load after animation finishes.
            setTimeout(function() {
                self.$modal.find('.modal-body').load(self.options.url, function() {
                    if(self.options.showBtns) {
                        self.$modal.find('.modal-footer').removeClass('hidden');
                    }
                    if (self.options.onLoad) {
                        self.options.onLoad();
                    }
                });
            }, 1000);
        }
        if (self.options.success) {
            self.$modal.find('.btn-primary').off('click').on('click', self.options.success);
        }
        self.$modal.modal("show");
    },

    $.fn.closemodal = function() {
        $('#primary-modal').modal("hide");
    },

    $.fn.refreshmodal = function(html) {
        $('#primary-modal').find('.modal-body').html(html);
    },

    $('[data-click="popup"]').click(function() {
        var self = $(this);
        var url = self.attr('data-url');
        var $modal = $('#primary-modal');
        self.openmodal({
            title: self.attr('data-title'),
            url: url,
            showBtns: true,
            success: function() {
                $modal.find('.btn-primary').addClass('btn-loading');
                var $form = $modal.find('form');
                if ($form.length) {
                    $.post($form.attr('action'), $form.serialize(), function(response) {
                        if (typeof response == 'object' && response != null) {
                            if (response.success === true) {
                                window.location.reload();
                            } else if (response.error !== undefined) {
                                $form.find('#errors').html(response.error);
                                $modal.find('.btn-primary').removeClass('btn-loading');
                            }
                        } else {
                            self.refreshmodal(response);
                            $modal.find('.btn-primary').removeClass('btn-loading');
                        }
                    });
                }
                return false;
            }
        });
        return false;
    });
})(jQuery);
